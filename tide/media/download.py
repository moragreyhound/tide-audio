"""Download queue: bounded concurrency, progress hooks, cover embedding.

Rules enforced here (see AGENTS.md):

* never overwrite silently - an existing target asks once per session
  (``on_exists = ask``) and remembers the answer;
* cancellations clean up ``.part``/``.ytdl`` leftovers;
* DRM'd or otherwise protected content fails fast with a clear message;
* retries: two attempts for downloads (spec), then a failed state with a
  message and ``r`` to retry manually.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from tide.log import get_logger
from tide.media.errors import classify_yt_dlp_error, first_error_line
from tide.media.ydl import apply_network, base_opts, open_ydl, with_retries
from tide.store.playlists import Track
from tide.textutil import sanitize_filename

log = get_logger(__name__)

ExistsAnswer = str  # "overwrite" | "skip" | "overwrite_all" | "skip_all"


class DownloadState(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    CONVERTING = "converting"
    DONE = "done"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def terminal(self) -> bool:
        return self in (DownloadState.DONE, DownloadState.FAILED, DownloadState.CANCELED)


class DownloadCancelled(Exception):
    """Raised from a progress hook when the user cancels."""


@dataclass
class DownloadItem:
    id: str
    source: str
    title: str
    channel: str = ""
    duration: int | None = None
    thumbnail: str | None = None
    state: DownloadState = DownloadState.QUEUED
    progress: float = 0.0          # 0..1
    speed: float | None = None     # bytes/s
    eta: int | None = None         # seconds
    message: str = ""
    target: str | None = None
    created: float = field(default_factory=time.monotonic)
    flash_until: float = 0.0       # completion flash timestamp for the UI

    @classmethod
    def from_track(cls, track: Track) -> "DownloadItem":
        return cls(
            id=uuid.uuid4().hex[:12],
            source=track.source,
            title=track.title or track.source,
            channel=track.channel,
            duration=track.duration,
            thumbnail=track.thumbnail,
        )

    @property
    def finished_at(self) -> float | None:
        return self.flash_until if self.state == DownloadState.DONE else None


class DownloadManager:
    """Thread-safe download queue; the UI polls :meth:`snapshot`."""

    def __init__(
        self,
        config: Any,
        *,
        on_event: Callable[[str, DownloadItem], None] | None = None,
        ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.config = config
        self.on_event = on_event
        self._ydl_factory = ydl_factory or open_ydl
        self._lock = threading.RLock()
        self._items: list[DownloadItem] = []
        self._pending: list[DownloadItem] = []
        self._cancel: set[str] = set()
        self._threads: list[threading.Thread] = []
        self._active = 0
        self._limit = max(1, int(getattr(config, "download_concurrency", 2) or 2))
        self._exists_events: dict[str, threading.Event] = {}
        self._exists_answers: dict[str, ExistsAnswer] = {}
        self._session_policy: ExistsAnswer | None = None
        configured = getattr(config, "on_exists", "ask")
        if configured in ("overwrite", "skip"):
            self._session_policy = configured
        self._closed = False
        self._ydl_created: dict[str, Any] = {}  # item id -> ydl (cleanup)

    # ------------------------------------------------------------------
    # Queue API
    # ------------------------------------------------------------------
    def add(self, tracks: Iterable[Track]) -> list[DownloadItem]:
        created: list[DownloadItem] = []
        with self._lock:
            for track in tracks:
                if "://" not in track.source and not track.source.startswith("file://"):
                    item = DownloadItem.from_track(track)
                    path = Path(track.source).expanduser()
                    item.state = DownloadState.FAILED
                    if path.exists():
                        item.message = "Local file already - nothing to download."
                    else:
                        item.message = "That local file does not exist."
                    created.append(item)
                    self._items.append(item)
                    continue
                item = DownloadItem.from_track(track)
                self._items.append(item)
                self._pending.append(item)
                created.append(item)
                self._spawn(item)
        for item in created:
            self._emit("changed", item)
        return created

    def retry(self, item_id: str) -> DownloadItem | None:
        with self._lock:
            item = self._find(item_id)
            if item is None or item.state not in (
                DownloadState.FAILED,
                DownloadState.CANCELED,
            ):
                return None
            item.state = DownloadState.QUEUED
            item.progress = 0.0
            item.speed = None
            item.eta = None
            item.message = ""
            item.target = None
            self._cancel.discard(item.id)
            self._pending.append(item)
            self._spawn(item)
        self._emit("changed", item)
        return item

    def cancel(self, item_id: str) -> bool:
        with self._lock:
            item = self._find(item_id)
            if item is None:
                return False
            if item.state == DownloadState.QUEUED:
                if item in self._pending:
                    self._pending.remove(item)
                item.state = DownloadState.CANCELED
                item.message = "Canceled"
                answer = self._exists_events.pop(item.id, None)
                if answer is not None:
                    self._exists_answers[item.id] = "skip"
                    answer.set()
                self._emit("changed", item)
                return True
            if item.state in (DownloadState.DOWNLOADING, DownloadState.CONVERTING):
                self._cancel.add(item.id)
                event = self._exists_events.get(item.id)
                if event is not None:
                    self._exists_answers[item.id] = "skip"
                    event.set()
                item.message = "Canceling..."
                return True
            return False

    def resolve_exists(self, answer: ExistsAnswer) -> None:
        """Answer the current "file exists" question (and remember all/skip)."""
        if answer in ("overwrite_all", "skip_all"):
            self._session_policy = "overwrite" if answer == "overwrite_all" else "skip"
        with self._lock:
            for item_id, event in list(self._exists_events.items()):
                self._exists_answers[item_id] = answer
                event.set()

    @property
    def needs_exists_answer(self) -> bool:
        with self._lock:
            return bool(self._exists_events)

    @property
    def exists_question(self) -> DownloadItem | None:
        """The item currently waiting for an overwrite decision."""
        with self._lock:
            for item_id in self._exists_events:
                item = self._find(item_id)
                if item is not None:
                    return item
        return None

    def snapshot(self) -> list[DownloadItem]:
        with self._lock:
            return list(self._items)

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active + len(self._pending)

    @property
    def done_count(self) -> int:
        with self._lock:
            return sum(1 for i in self._items if i.state == DownloadState.DONE)

    def wait_idle(self, timeout: float | None = None) -> bool:
        """Block until every item is terminal (CLI usage)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                if not self._pending and self._active == 0:
                    if not any(i.state == DownloadState.QUEUED for i in self._items):
                        return True
            if deadline is not None and time.monotonic() > deadline:
                return False
            time.sleep(0.05)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for event in self._exists_events.values():
                event.set()
            self._exists_events.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _find(self, item_id: str) -> DownloadItem | None:
        for item in self._items:
            if item.id == item_id:
                return item
        return None

    def _emit(self, kind: str, item: DownloadItem) -> None:
        if self.on_event is not None:
            try:
                self.on_event(kind, item)
            except Exception:  # pragma: no cover - listeners must not break downloads
                pass

    def _spawn(self, item: DownloadItem) -> None:
        with self._lock:
            self._active += 1
        thread = threading.Thread(
            target=self._run_slot, args=(item,), name=f"tide-dl-{item.id}", daemon=True
        )
        self._threads.append(thread)
        thread.start()

    def _run_slot(self, item: DownloadItem) -> None:
        """Concurrency gate: only ``_limit`` downloads run at once."""
        semaphore = self._get_semaphore()
        semaphore.acquire()
        try:
            if self._closed or item.id in self._cancel:
                with self._lock:
                    if not item.state.terminal:
                        item.state = DownloadState.CANCELED
                        item.message = "Canceled"
                self._emit("changed", item)
                return
            if item.state == DownloadState.QUEUED:
                self._process(item)
        finally:
            semaphore.release()
            with self._lock:
                self._active -= 1

    _semaphore: threading.Semaphore | None = None

    def _get_semaphore(self) -> threading.Semaphore:
        if self._semaphore is None:
            self._semaphore = threading.Semaphore(self._limit)
        return self._semaphore

    # -- the actual work -------------------------------------------------
    def _process(self, item: DownloadItem) -> None:
        with self._lock:
            item.state = DownloadState.DOWNLOADING
            item.progress = 0.0
            item.message = ""
        self._emit("changed", item)

        opts = self._build_opts(item)
        try:
            info = self._extract(item, opts)
            if item.id in self._cancel:
                raise DownloadCancelled()
            target = self._decide_target(item, opts, info, item.id)
            if target is None:  # skip
                with self._lock:
                    item.state = DownloadState.DONE
                    item.progress = 1.0
                    item.message = "kept the existing file"
                self._emit("changed", item)
                return
            with self._lock:
                item.state = DownloadState.CONVERTING
                item.progress = 1.0
                item.speed = None
                item.eta = None
            self._emit("changed", item)

            self._download_with_info(item, opts, info)
            final_path = self._final_path(opts, info, target)
            with self._lock:
                item.state = DownloadState.DONE
                item.progress = 1.0
                item.target = str(final_path) if final_path else None
                item.flash_until = time.monotonic() + 0.6
                item.message = ""
            self._emit("changed", item)
        except DownloadCancelled:
            with self._lock:
                item.state = DownloadState.CANCELED
                item.message = "Canceled"
                item.progress = 0.0
            self._cleanup_partials(item, opts)
            self._emit("changed", item)
        except Exception as exc:
            technical = first_error_line(str(exc))
            message = classify_yt_dlp_error(technical) or (
                f"Download failed for {item.title[:48]}: {technical[:140]}"
            )
            with self._lock:
                item.state = DownloadState.FAILED
                item.message = message
            self._cleanup_partials(item, opts)
            log.warning("download failed (%s): %s", item.source, technical)
            self._emit("changed", item)

    def _build_opts(self, item: DownloadItem) -> dict[str, Any]:
        config = self.config
        music_dir = Path(getattr(config, "music_dir", Path.home() / "Music")).expanduser()
        try:
            music_dir.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - read-only media
            pass
        template = getattr(config, "download_template", "%(title)s.%(ext)s")
        outtmpl = str(music_dir / template)
        fmt = getattr(config, "download_format", "mp3")
        bitrate = str(getattr(config, "download_bitrate", "192"))

        postprocessors: list[dict[str, Any]] = []
        if fmt in ("mp3", "m4a", "opus", "flac", "wav"):
            pp: dict[str, Any] = {"key": "FFmpegExtractAudio", "preferredcodec": fmt}
            if fmt == "mp3" and bitrate != "best":
                pp["preferredquality"] = bitrate
            postprocessors.append(pp)
        postprocessors.append({"key": "FFmpegMetadata", "add_metadata": True})
        embed_cover = fmt != "wav"  # WAV has no standard cover-art slot
        if embed_cover:
            postprocessors.append({"key": "EmbedThumbnail"})

        opts = base_opts(
            {
                "format": "bestaudio/best",
                "noplaylist": True,
                "outtmpl": outtmpl,
                "windowsfilenames": True,  # sanitize for the strictest platform
                "noprogress": True,
                "concurrent_fragment_downloads": 4,
                "retries": 2,
                "fragment_retries": 3,
                "postprocessors": postprocessors,
                "progress_hooks": [self._progress_hook(item)],
                "overwrites": self._session_policy == "overwrite",
            }
        )
        if getattr(config, "writethumbnail", True) and embed_cover:
            opts["writethumbnail"] = True
        apply_network(opts, config)
        return opts

    def _progress_hook(self, item: DownloadItem) -> Callable[[dict[str, Any]], None]:
        def hook(status: dict[str, Any]) -> None:
            if item.id in self._cancel:
                raise DownloadCancelled()
            kind = status.get("status")
            if kind == "downloading":
                total = status.get("total_bytes") or status.get("total_bytes_estimate")
                downloaded = status.get("downloaded_bytes") or 0
                with self._lock:
                    if total:
                        item.progress = max(0.0, min(1.0, float(downloaded) / float(total)))
                    item.speed = (
                        float(status["speed"]) if isinstance(status.get("speed"), (int, float)) else None
                    )
                    item.eta = (
                        int(status["eta"]) if isinstance(status.get("eta"), (int, float)) else None
                    )
                    if item.state == DownloadState.DOWNLOADING:
                        pass
                self._emit("changed", item)
            elif kind == "finished":
                with self._lock:
                    item.progress = 1.0
                    item.speed = None
                    item.eta = None
                    item.state = DownloadState.CONVERTING
                    item.message = ""
                self._emit("changed", item)

        return hook

    def _extract(self, item: DownloadItem, opts: dict[str, Any]) -> dict[str, Any]:
        def run() -> dict[str, Any]:
            with self._ydl_factory(opts) as ydl:
                info = ydl.extract_info(item.source, download=False)
            if not isinstance(info, dict):
                raise RuntimeError("yt-dlp returned nothing for that link")
            formats = info.get("formats")
            if isinstance(formats, list) and any(
                isinstance(f, dict) and f.get("drm") for f in formats
            ):
                raise RuntimeError("protected by DRM; Tide cannot download this")
            if info.get("drm"):
                raise RuntimeError("protected by DRM; Tide cannot download this")
            return info

        return with_retries(run, attempts=2)

    def _evaluate_outtmpl(self, opts: dict[str, Any], info: dict[str, Any]) -> Path | None:
        outtmpl = opts.get("outtmpl")
        if not isinstance(outtmpl, str):
            return None
        try:
            with self._ydl_factory({**opts, "quiet": True}) as ydl:
                rendered = ydl.evaluate_outtmpl(outtmpl, info)
        except Exception:
            # Fallback: render the common fields ourselves.
            title = sanitize_filename(str(info.get("title") or "download"))
            ext = str(info.get("ext") or "webm")
            try:
                rendered = outtmpl % {
                    "artist": sanitize_filename(str(info.get("artist") or info.get("uploader") or "Unknown")),
                    "uploader": sanitize_filename(str(info.get("uploader") or "Unknown")),
                    "album": sanitize_filename(str(info.get("album") or "Singles")),
                    "playlist_title": sanitize_filename(str(info.get("playlist_title") or "Singles")),
                    "title": title,
                    "ext": ext,
                }
            except (KeyError, ValueError, TypeError):
                return None
        if not isinstance(rendered, str) or not rendered:
            return None
        return Path(rendered)

    def _final_path(self, opts: dict[str, Any], info: dict[str, Any], base: Path) -> Path:
        fmt = getattr(self.config, "download_format", "mp3")
        if base.suffix.lower().lstrip(".") != fmt.lower():
            return base.with_suffix(f".{fmt}")
        return base

    def _decide_target(
        self, item: DownloadItem, opts: dict[str, Any], info: dict[str, Any], item_id: str
    ) -> Path | None:
        """Return the base target, or ``None`` when the file should be skipped."""
        base = self._evaluate_outtmpl(opts, info)
        if base is None:
            return None
        final = self._final_path(opts, info, base)
        if not final.exists():
            return final
        if self._session_policy == "overwrite":
            return final
        if self._session_policy == "skip":
            log.debug("exists, skipping: %s", final)
            return None
        # ask once per session, wait for the UI/CLI answer
        event = threading.Event()
        with self._lock:
            self._exists_events[item.id] = event
        self._emit("exists", item)
        event.wait(timeout=None)
        with self._lock:
            self._exists_events.pop(item.id, None)
            answer = self._exists_answers.pop(item.id, "skip")
        if answer in ("overwrite_all", "overwrite"):
            self._session_policy = self._session_policy or None
            if answer == "overwrite_all":
                self._session_policy = "overwrite"
            return final
        return None

    def _download_with_info(self, item: DownloadItem, opts: dict[str, Any], info: dict[str, Any]) -> None:
        def run() -> None:
            with self._ydl_factory(opts) as ydl:
                if hasattr(ydl, "process_ie_result"):
                    ydl.process_ie_result(info, download=True)
                else:  # pragma: no cover - older yt-dlp
                    ydl.download([item.source])

        with_retries(run, attempts=2)

    def _cleanup_partials(self, item: DownloadItem, opts: dict[str, Any]) -> None:
        """Remove ``.part`` / ``.ytdl`` leftovers for this attempt."""
        outtmpl = opts.get("outtmpl")
        if not isinstance(outtmpl, str):
            return
        music_dir = Path(getattr(self.config, "music_dir", Path.home() / "Music"))
        pattern_dir = music_dir
        stem = Path(outtmpl).name
        try:
            matches = list(pattern_dir.glob("**/" + stem.replace("%(", "*(") + "*"))
        except OSError:  # pragma: no cover
            return
        for path in matches:
            name = path.name
            if name.endswith(".part") or name.endswith(".ytdl") or ".part-Frag" in name:
                try:
                    path.unlink()
                except OSError:  # pragma: no cover
                    pass
