"""Resolution: turn a source (URL, local path, playlist) into playable data.

* :func:`resolve_stream` - full extraction, best audio URL + HTTP headers
  (expiring values are returned to the engine, never persisted).
* :func:`expand_source` - playlist/album expansion with flat extraction and
  progress callbacks (``14 of 60 tracks resolved``), cancellable.
* Local files skip yt-dlp entirely.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from tide.log import get_logger
from tide.media.errors import classify_yt_dlp_error, first_error_line
from tide.media.ydl import apply_network, base_opts, is_playlist_url, open_ydl, with_retries
from tide.store.playlists import Track

log = get_logger(__name__)


class ResolveError(RuntimeError):
    """Resolution failed with a user-ready message attached."""

    def __init__(self, message: str, *, technical: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.technical = technical or message


@dataclass
class Resolved:
    """A playable stream (URL is expiring - engine uses it, store never does)."""

    url: str
    http_headers: dict[str, str] | None = None
    duration: int | None = None
    title: str = ""
    thumbnail: str | None = None
    is_local: bool = False
    source: str = ""

    @classmethod
    def local(cls, path: str, source: str) -> "Resolved":
        return cls(url=path, http_headers=None, duration=None, is_local=True, source=source)


def local_path_for(source: str) -> Path | None:
    """Return an existing local path for ``source`` or ``None`` (it's remote)."""
    if "://" in source:
        if not source.startswith("file://"):
            return None
        candidate = Path(source[7:])
    else:
        candidate = Path(source).expanduser()
    try:
        return candidate if candidate.is_file() else None
    except OSError:  # pragma: no cover - exotic paths
        return None


def build_resolve_opts(config: Any = None) -> dict[str, Any]:
    opts = base_opts(
        {
            "format": "bestaudio/best",
            "noplaylist": True,
            "ignoreerrors": False,
        }
    )
    return apply_network(opts, config)


def parse_formats(info: dict[str, Any]) -> tuple[str, dict[str, str] | None, int | None, str | None]:
    """Pick ``(url, headers, duration, error)`` from an extraction result.

    Refuses DRM'd formats explicitly and prefers the top audio-only entry.
    """
    formats = info.get("formats")
    if not isinstance(formats, list) or not formats:
        url = info.get("url")
        if isinstance(url, str) and url and not info.get("drm"):
            headers = info.get("http_headers")
            duration = info.get("duration")
            return (
                url,
                dict(headers) if isinstance(headers, dict) else None,
                int(duration) if isinstance(duration, (int, float)) else None,
                None,
            )
        return (None, None, None, "no playable stream (the page may need an update)")

    playable = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        if fmt.get("drm"):
            continue
        if not fmt.get("url"):
            continue
        playable.append(fmt)
    if not playable:
        if any(isinstance(f, dict) and f.get("drm") for f in formats):
            return (None, None, None, "protected by DRM; Tide cannot play this")
        return (None, None, None, "no playable stream (the page may need an update)")

    def sort_key(fmt: dict[str, Any]) -> tuple[int, int]:
        vcodec = fmt.get("vcodec")
        audio_only = 1 if vcodec in (None, "none") else 0
        abr = fmt.get("abr") or 0
        return (audio_only, int(abr) if isinstance(abr, (int, float)) else 0)

    playable.sort(key=sort_key, reverse=True)
    chosen = playable[0]
    duration = info.get("duration")
    headers = chosen.get("http_headers") or info.get("http_headers")
    return (
        str(chosen["url"]),
        dict(headers) if isinstance(headers, dict) else None,
        int(duration) if isinstance(duration, (int, float)) else None,
        None,
    )


def resolve_stream(source: str, config: Any = None, *, attempts: int = 3) -> Resolved:
    """Resolve one source to a playable stream URL (network call)."""
    local = local_path_for(source)
    if local is not None:
        return Resolved.local(str(local), source)

    opts = build_resolve_opts(config)
    factory = open_ydl

    def run() -> Resolved:
        with factory(opts) as ydl:
            info = ydl.extract_info(source, download=False)
        if not isinstance(info, dict):
            raise ResolveError("yt-dlp returned nothing for that link.", technical=str(info))
        url, headers, duration, error = parse_formats(info)
        if error:
            raise ResolveError(
                _sentence_for(error, info), technical=first_error_line(str(info.get("title", error)))
            )
        return Resolved(
            url=url,
            http_headers=headers,
            duration=duration,
            title=str(info.get("title") or ""),
            thumbnail=_thumbnail_of(info),
            source=source,
        )

    try:
        return with_retries(run, attempts=attempts)
    except ResolveError:
        raise
    except Exception as exc:  # yt-dlp raises many types
        raise ResolveError(_sentence_for(str(exc)), technical=str(exc)) from exc


def _thumbnail_of(info: dict[str, Any]) -> str | None:
    if isinstance(info.get("thumbnail"), str):
        return str(info["thumbnail"])
    thumbnails = info.get("thumbnails")
    if isinstance(thumbnails, list) and thumbnails:
        from tide.media.thumbnails import best_thumbnail_url

        return best_thumbnail_url(thumbnails)
    return None


def _sentence_for(text: str, info: dict[str, Any] | None = None) -> str:
    classified = classify_yt_dlp_error(text)
    if classified:
        return classified
    if info is not None:
        title = str(info.get("title") or "that track")
        return f"Could not read {title}: {first_error_line(text)[:120]}"
    return f"Could not read that link: {first_error_line(text)[:120]}"


def _id_from_url(url: str) -> str | None:
    if "watch?v=" in url:
        parsed = parse_qs(urlparse(url).query)
        values = parsed.get("v")
        return values[0] if values else None
    if "youtu.be/" in url:
        return url.rsplit("/", 1)[-1].split("?", 1)[0] or None
    return None


def video_source(entry: dict[str, Any]) -> str | None:
    """Stable watch URL for a flat playlist/search entry."""
    from tide.media.search import _watch_url

    return _watch_url(entry)


def expand_source(
    source: str,
    config: Any = None,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
    attempts: int = 2,
) -> list[Track]:
    """Expand a playlist (or single URL) into ordered tracks, flat.

    ``progress(done, total)`` fires per entry; ``cancel()`` is polled between
    entries and stops cleanly (raising :class:`ExpandCancelled`).
    """
    local = local_path_for(source)
    if local is not None:
        return [Track(source=str(local), title=local.stem)]

    opts = base_opts(
        {
            "extract_flat": True,
            "noplaylist": False,
            "playlistend": 1000,
            "ignoreerrors": True,
        }
    )
    apply_network(opts, config)
    factory = open_ydl

    def run() -> list[Track]:
        with factory(opts) as ydl:
            info = ydl.extract_info(source, download=False, process=False)
        return _tracks_from_info(info, progress=progress, cancel=cancel)

    try:
        return with_retries(run, attempts=attempts)
    except Exception as exc:
        raise ResolveError(_sentence_for(str(exc)), technical=str(exc)) from exc


class ExpandCancelled(RuntimeError):
    """User cancelled an import mid-flight."""


def _tracks_from_info(
    info: Any,
    *,
    progress: Callable[[int, int], None] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> list[Track]:
    if not isinstance(info, dict):
        return []
    entries = info.get("entries")
    tracks: list[Track] = []
    if not isinstance(entries, list):
        track = _track_from_entry(info)
        return [track] if track else []

    total = info.get("playlist_count") or len(entries)
    resolved_count = 0
    for raw in entries:
        if cancel is not None and cancel():
            raise ExpandCancelled("import cancelled")
        if raw is None:
            continue
        resolved_count += 1
        if isinstance(raw, dict):
            track = _track_from_entry(raw)
            if track:
                tracks.append(track)
        if progress is not None:
            progress(resolved_count, int(total) if isinstance(total, (int, float)) else len(entries))
    return tracks


def _track_from_entry(entry: dict[str, Any]) -> Track | None:
    source = video_source(entry)
    if not source:
        return None
    duration = entry.get("duration")
    if not isinstance(duration, (int, float)) or isinstance(duration, bool):
        duration = None
    thumbnail = entry.get("thumbnail")
    if not thumbnail:
        thumbnails = entry.get("thumbnails")
        if isinstance(thumbnails, list) and thumbnails:
            from tide.media.thumbnails import best_thumbnail_url

            thumbnail = best_thumbnail_url(thumbnails)
    return Track(
        source=source,
        title=str(entry.get("title") or source),
        channel=str(entry.get("channel") or entry.get("uploader") or ""),
        duration=int(duration) if duration is not None else None,
        thumbnail=str(thumbnail) if isinstance(thumbnail, str) else None,
    )


def should_expand(source: str) -> bool:
    """True when the source may represent multiple tracks."""
    if is_playlist_url(source):
        return True
    return False
