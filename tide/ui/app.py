"""Tide application: three-zone layout, key routing, engine event pump.

Responsibilities kept here (and nowhere else):

* own the engine, stores, downloads and ambient theme, wiring them together;
* route every key through :meth:`Keymap.action_for` with the focused context;
* pump engine events once per tick and repaint only what actually changed.

Widgets never touch the engine directly; they receive prepared data.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Any, Callable

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static

from tide.audio.engine import PlaybackEngine
from tide.audio.eq import PRESETS, preset_gains
from tide.config import Config, LoadedConfig, load_config
from tide.keys import ACTIONS, Keymap, load_keymap, normalize_key
from tide.log import get_logger
from tide.media.download import DownloadManager, DownloadState
from tide.media.errors import classify_yt_dlp_error, first_error_line
from tide.media.resolve import resolve_stream
from tide.media.search import SearchCache, search as run_search
from tide.media.thumbnails import fetch_thumbnail
from tide.paths import Paths, ensure_dirs, get_paths
from tide.store.history import SearchHistory
from tide.store.playlists import PlaylistStore, Track
from tide.store.session import SessionState, SessionStore
from tide.textutil import format_duration
from tide.ui.screens.overlays import HelpScreen, PickerScreen, PromptScreen
from tide.ui.widgets import (
    DownloadsPanel,
    Header,
    ListRow,
    NowPlaying,
    StatusBar,
    TrackList,
    VisualizerWidget,
)
from tide.ui.widgets.rail import Rail, RailEntry
from tide.visual.ambient import AmbientTheme
from tide.visual.color import hex_to_rgb
from tide.visual.palette import Palette, extract_palette_cached

log = get_logger(__name__)

TICK_INTERVAL = 1 / 20       # full motion: 20 fps pump
TICK_SLOW = 1 / 10           # reduced / none: a lighter pump
BREATH_INTERVAL = 0.45       # buffering dots cadence
PAINT_INTERVAL = 0.2         # downloads panel repaint throttle
COUNTS_INTERVAL = 0.5        # status hints throttle
LIBRARY_LIMIT = 5000
AUDIO_SUFFIXES = {
    ".mp3", ".m4a", ".flac", ".wav", ".ogg", ".opus", ".aac", ".wma", ".aiff",
}

PLAY_ACTIONS = {"row_play", "playlist_play"}  # dispatched via OptionSelected


class TideApp(App[None]):
    """The main (and only) screen, plus modal overlays."""

    CSS_PATH = "theme.tcss"
    TITLE = "Tide"

    def __init__(
        self,
        *,
        config: Config | None = None,
        paths: Paths | None = None,
        keymap: Keymap | None = None,
        engine: PlaybackEngine | None = None,
        downloads: DownloadManager | None = None,
        start_url: str | None = None,
    ) -> None:
        super().__init__()
        self.paths = paths or get_paths()
        ensure_dirs(self.paths)
        loaded: LoadedConfig | None = None
        if config is None:
            loaded = load_config(self.paths)
            config = loaded.config
        self.config = config
        self.config_issues = loaded.issues if loaded else []
        self.keymap = keymap or load_keymap(self.paths)
        self.engine = engine or PlaybackEngine(
            self.config, resolver=lambda src: resolve_stream(src, self.config)
        )
        self.start_url = start_url

        self.session_store = SessionStore(self.paths)
        self.history = SearchHistory(self.paths)
        self.playlists = PlaylistStore(self.paths)
        self.search_cache = SearchCache()
        self.downloads = downloads or DownloadManager(
            self.config, on_event=self._download_event
        )
        motion = getattr(self.config, "motion", "full")
        self.ambient = AmbientTheme(motion=motion)

        # view state
        self.view = "search"
        self.search_tracks: list[Track] = []
        self.search_query = ""
        self.library_tracks: list[Track] = []
        self.playlist_id: str | None = None
        self.playlist_name = ""
        self.rail_visible = True
        self.downloads_visible = False

        # flow state
        self._closing = False
        self._last_tick = time.monotonic()
        self._was_active = False
        self._breath = 0
        self._breath_last = 0.0
        self._palette_token: str | None = None
        self._search_seq = 0
        self._pending_start: float | None = None
        self._exists_open = False
        self._dl_dirty = False
        self._dl_last_paint = 0.0
        self._counts_last = 0.0
        self._eq_gains: list[float] = [0.0] * 10
        self._head_key: tuple = ()

    # ==================================================================
    # Composition
    # ==================================================================
    def compose(self) -> ComposeResult:
        yield Header(id="header")
        yield Horizontal(
            Rail(id="rail"),
            Vertical(
                Static("", id="listhead"),
                TrackList(id="list"),
                id="center",
            ),
            id="body",
        )
        yield DownloadsPanel(id="downloads")
        yield NowPlaying(id="nowplaying")
        yield VisualizerWidget(
            mode_name=getattr(self.config, "visualizer_default", "wash"),
            motion=getattr(self.config, "motion", "full"),
            id="visualizer",
        )
        yield StatusBar(id="status")

    def on_mount(self) -> None:
        self._restore_session()
        self.engine.start()
        motion = getattr(self.config, "motion", "full")
        interval = TICK_INTERVAL if motion == "full" else TICK_SLOW
        self.set_interval(interval, self._tick)

        self._apply_theme()
        self._refresh_rail()
        self._show_view(self.view)
        self.query_one("#list").focus()

        status = self.query_one(StatusBar)
        status.set_message("Press / to search, f1 for keys.", ttl=8.0)
        for issue in self.config_issues:
            log.warning("config issue: %s", issue)
        if self.start_url:
            self._play_source(self.start_url)

    # ==================================================================
    # Tick: events, ambient, widgets
    # ==================================================================
    def _tick(self) -> None:
        if self._closing:
            return
        now = time.monotonic()
        dt = now - self._last_tick
        self._last_tick = now

        new_frame = None
        for event in self.engine.drain_events():
            kind = event.type
            if kind == "frame":
                new_frame = event.data.get("frame") or new_frame
            elif kind == "state":
                self._on_state(event.data)
            elif kind == "track":
                self._on_track(event.data)
            elif kind == "error":
                self._on_error(event.data)
            elif kind == "info":
                message = str(event.data.get("message") or "")
                if message:
                    self.query_one(StatusBar).set_message(message, ttl=5.0)
            elif kind == "device":
                message = str(event.data.get("message") or event.data.get("status") or "")
                if message:
                    self.query_one(StatusBar).set_message(message, ttl=5.0)

        snapshot = self.engine.snapshot()
        active = snapshot.is_active

        viz = self.query_one(VisualizerWidget)
        if new_frame is not None and active:
            viz.feed(new_frame, now=now)
        elif not active and self._was_active:
            viz.feed(None, now=now)
        self._was_active = active
        viz.render_if_dirty(now=now)

        if self.ambient.advance(dt):
            self._apply_theme()

        # buffering breath
        if snapshot.state.value == "buffering" and self.ambient.motion == "full":
            if now - self._breath_last >= BREATH_INTERVAL:
                self._breath_last = now
                self._breath += 1
                self._paint_nowplaying(snapshot)
        else:
            self._breath = 0
        self._paint_nowplaying(snapshot)

        if now - self._dl_last_paint >= PAINT_INTERVAL:
            self._dl_last_paint = now
            self._paint_downloads(now)
        if now - self._counts_last >= COUNTS_INTERVAL:
            self._counts_last = now
            self._paint_counts(snapshot)

    # -- engine events ---------------------------------------------------
    def _on_state(self, data: dict[str, Any]) -> None:
        state = str(data.get("state") or "idle")
        self.query_one(Header).set_state(state)
        if state == "ended":
            self.query_one(StatusBar).set_message("Ended.", ttl=3.0, kind="dim")
        elif state == "idle":
            self.query_one(StatusBar).set_message("Stopped.", ttl=2.0, kind="dim")

    def _on_track(self, data: dict[str, Any]) -> None:
        track = data.get("track")
        if not isinstance(track, Track):
            return
        self.query_one(NowPlaying).show_track(track)
        self.query_one(TrackList).set_current(track.source)
        detail = f"{track.title} - {track.channel}" if track.channel else track.title
        self.query_one(StatusBar).set_message(detail[:80], ttl=4.0, kind="dim")
        self.query_one(VisualizerWidget).on_track_change()
        self._start_palette(track)

    def _on_error(self, data: dict[str, Any]) -> None:
        message = str(data.get("message") or "Playback failed.")
        self.query_one(StatusBar).set_message(message, ttl=8.0, kind="danger")

    # ==================================================================
    # Theme
    # ==================================================================
    def _apply_theme(self) -> None:
        colors = self.ambient.colors
        palette = self.ambient.palette
        self.query_one(Header).set_colors(colors)
        self.query_one(TrackList).set_colors(colors)
        self.query_one(Rail).set_colors(colors)
        self.query_one(DownloadsPanel).set_colors(colors)
        self.query_one(NowPlaying).set_colors(colors)
        self.query_one(StatusBar).set_colors(colors)
        self.query_one(VisualizerWidget).set_colors(colors, palette)
        for selector in (
            "#body", "#center", "#rail", "#downloads", "#nowplaying",
            "#visualizer", "#status", "#header", "#listhead",
        ):
            try:
                self.query_one(selector).styles.background = colors.background
            except Exception:  # pragma: no cover - widget not mounted yet
                pass
        self._paint_listhead(force=True)

    def _start_palette(self, track: Track) -> None:
        source = track.source
        if source == self._palette_token:
            return
        self._palette_token = source
        thumb = track.thumbnail
        use_thumb = (
            thumb is not None and getattr(self.config, "ambient_source", "thumbnail") == "thumbnail"
        )
        override = getattr(self.config, "accent_override", None)

        def work() -> None:
            palette: Palette | None = None
            if use_thumb and thumb:
                data = fetch_thumbnail(thumb, config=self.config, paths=self.paths)
                if data:
                    palette = extract_palette_cached(data, self.paths)
            if palette is None:
                palette = Palette.fallback()
            if override:
                try:
                    accent = hex_to_rgb(override)
                    palette = Palette(
                        colors=palette.colors,
                        accent=accent,
                        dominant=palette.dominant,
                        background=palette.background,
                        background_light=palette.background_light,
                        source_hash=palette.source_hash,
                    )
                except ValueError:
                    log.warning("invalid accent_override %r", override)
            self.call_from_thread(self._apply_palette, source, palette)

        self._spawn(f"palette-{source[:24]}", work)

    def _apply_palette(self, source: str, palette: Palette) -> None:
        if source != self._palette_token or self._closing:
            return
        self.ambient.set_palette(palette)
        if not self.ambient.transitioning:
            self._apply_theme()

    # ==================================================================
    # Views and rows
    # ==================================================================
    def _show_view(self, view: str) -> None:
        if view == "playlist" and self.playlist_id is None:
            view = "queue"
        self.view = view
        header = self.query_one(Header)
        header.set_view(view)
        rows, head = self._build_rows(view)
        self.query_one(TrackList).set_rows(
            rows, current_source=self._current_source()
        )
        self._head_key = (view, head, len(rows))
        self._paint_listhead()

    def _build_rows(self, view: str) -> tuple[list[Any], str]:
        """Return ``(rows, head_key)``; the head text is painted separately."""
        if view == "search":
            rows = [ListRow(track=t, kind="result") for t in self.search_tracks]
            if not self.search_tracks:
                head = f"Search: {self.search_query or '(type a query above)'}"
            else:
                head = f"Search: {self.search_query} - {len(rows)} results"
            return rows, head
        if view == "queue":
            snapshot = self.engine.snapshot()
            rows = [ListRow(track=t, kind="queue") for t in snapshot.queue]
            return rows, f"Queue - {len(rows)} tracks"
        if view == "library":
            rows = [ListRow(track=t, kind="library") for t in self.library_tracks]
            if not rows:
                directory = Path(getattr(self.config, "music_dir", Path.home() / "Music"))
                return rows, f"Library: no audio files in {directory}"
            return rows, f"Library - {len(rows)} files"
        # playlist
        rows, name = self._playlist_rows()
        return rows, f"{name} - {len(rows)} tracks"

    def _playlist_rows(self) -> tuple[list[Any], str]:
        if not self.playlist_id:
            return [], "Playlist"
        playlist = self.playlists.get(self.playlist_id)
        if playlist is None:
            self.playlist_id = None
            return [], "Playlist"
        self.playlist_name = playlist.name
        return (
            [ListRow(track=t, kind="playlist") for t in playlist.tracks],
            playlist.name,
        )

    def _paint_listhead(self, *, force: bool = False) -> None:
        if not self._head_key:
            if not force:
                return
            head = ""
        else:
            head = self._head_key[1]
        colors = self.ambient.colors
        text = Text(head, style=colors.dim if colors else "dim")
        self.query_one("#listhead", Static).update(text)

    def _refresh_rows(self, *, keep_highlight: bool = True) -> None:
        """Rebuild the current view's rows after a mutation."""
        list_widget = self.query_one(TrackList)
        previous = list_widget.highlighted
        self._show_view(self.view)
        if keep_highlight and previous is not None:
            list_widget.select_index(previous)

    def _current_source(self) -> str:
        snapshot = self.engine.snapshot()
        if snapshot.track is not None:
            return snapshot.track.source
        return ""

    def _scan_library(self) -> None:
        root = Path(getattr(self.config, "music_dir", Path.home() / "Music"))
        tracks: list[Track] = []
        if root.is_dir():
            try:
                for path in sorted(root.rglob("*")):
                    if path.suffix.lower() not in AUDIO_SUFFIXES or not path.is_file():
                        continue
                    relative = path.relative_to(root)
                    parts = list(relative.parts[:-1])
                    tracks.append(
                        Track(
                            source=str(path),
                            title=path.stem,
                            channel=" / ".join(parts),
                        )
                    )
                    if len(tracks) >= LIBRARY_LIMIT:
                        break
            except OSError as exc:
                log.warning("library scan failed: %s", exc)
        self.library_tracks = tracks

    # ==================================================================
    # Search
    # ==================================================================
    def _submit_search(self, query: str) -> None:
        query = query.strip()
        if not query:
            return
        self.history.add(query)
        self._history_pos = None
        self._search_seq += 1
        sequence = self._search_seq
        self.search_query = query
        self._show_view("search")
        self.query_one("#listhead", Static).update(Text(f"Searching: {query}"))
        self.query_one(StatusBar).set_message(f"Searching for {query}...", ttl=None)

        count = int(getattr(self.config, "search_count", 20))

        def work() -> None:
            try:
                results = run_search(
                    query, count=count, config=self.config, cache=self.search_cache
                )
                tracks = [
                    Track(
                        source=item.source,
                        title=item.title,
                        channel=item.channel,
                        duration=item.duration,
                        thumbnail=item.thumbnail,
                    )
                    for item in results
                ]
                self.call_from_thread(self._search_done, sequence, query, tracks, None)
            except Exception as exc:
                technical = first_error_line(str(exc))
                message = classify_yt_dlp_error(technical) or (
                    f"Search failed: {technical[:120]}"
                )
                self.call_from_thread(self._search_done, sequence, query, [], message)

        self._spawn("search", work)

    def _search_done(
        self, sequence: int, query: str, tracks: list[Track], error: str | None
    ) -> None:
        if sequence != self._search_seq or self._closing:
            return
        status = self.query_one(StatusBar)
        if error:
            status.set_message(error, ttl=8.0, kind="danger")
            self._paint_listhead(force=True)
            return
        self.search_tracks = tracks
        self._show_view("search")
        noun = "result" if len(tracks) == 1 else "results"
        status.set_message(
            f"{len(tracks)} {noun} for {query}." if tracks else f"No results for {query}.",
            ttl=5.0,
            kind="accent" if tracks else "dim",
        )

    # ==================================================================
    # Playback helpers
    # ==================================================================
    def _play_source(self, source: str) -> None:
        source = source.strip()
        if not source:
            return
        self.query_one(StatusBar).set_message(f"Loading {source[:60]}...", ttl=None)

        def work() -> None:
            try:
                resolved = resolve_stream(source, self.config)
                if resolved.is_local:
                    title = Path(resolved.url).stem
                    channel = ""
                else:
                    title = resolved.title or source
                    channel = ""
                track = Track(
                    source=source,
                    title=title,
                    channel=channel,
                    duration=resolved.duration,
                    thumbnail=resolved.thumbnail,
                )
                self.call_from_thread(self._play_resolved, track)
            except Exception as exc:
                technical = first_error_line(str(exc))
                message = getattr(exc, "message", None) or classify_yt_dlp_error(
                    technical
                ) or f"Could not open that link: {technical[:120]}"
                self.call_from_thread(self._play_failed, message)

        self._spawn("resolve", work)

    def _play_resolved(self, track: Track) -> None:
        if self._closing:
            return
        self.engine.play_track(track, queue=[track], index=0)
        self.query_one(StatusBar).set_message(f"Playing {track.title[:60]}", ttl=4.0)

    def _play_failed(self, message: str) -> None:
        if self._closing:
            return
        self.query_one(StatusBar).set_message(message, ttl=8.0, kind="danger")

    def _play_row(self, index: int) -> None:
        rows = self.query_one(TrackList).rows
        if not (0 <= index < len(rows)):
            return
        row = rows[index]
        if self.view == "queue":
            self.engine.play(index, start_at=self._consume_start())
        else:
            tracks = [r.track for r in rows]
            self.engine.set_queue(tracks, index)
            self.engine.play(index, start_at=self._consume_start())
        self.query_one(StatusBar).set_message(
            f"Playing {row.track.title[:60]}", ttl=4.0
        )

    def _consume_start(self) -> float:
        start = self._pending_start
        self._pending_start = None
        return start or 0.0

    # ==================================================================
    # Key routing
    # ==================================================================
    def on_key(self, event: events.Key) -> None:
        if self._closing:
            return
        key = normalize_key(event.key)
        context = self._key_context()
        action = self.keymap.action_for(key, context)
        if action is None:
            return
        if action in PLAY_ACTIONS:
            # enter/click are delivered through OptionSelected (single fire)
            return
        if isinstance(self.focused, Input) and ACTIONS.get(action, ("", "", ""))[2] != "overlay":
            return  # typing: only overlay-group keys may interrupt the Input
        handler = getattr(self, f"_act_{action}", None)
        if handler is None:
            return
        handler()
        event.stop()

    def _key_context(self) -> str | None:
        if isinstance(self.screen, ModalScreen):
            return "overlay"
        widget = self.focused
        while widget is not None:
            if isinstance(widget, Input):
                return "overlay"
            if isinstance(widget, TrackList):
                return "results"
            if isinstance(widget, Rail):
                return "rail"
            if isinstance(widget, DownloadsPanel):
                return "downloads"
            widget = getattr(widget, "parent", None)
        return None

    # -- global ----------------------------------------------------------
    def _act_quit(self) -> None:
        self._shutdown()
        self.exit()

    def _act_help(self) -> None:
        self.push_screen(HelpScreen(self.keymap, self.ambient.colors))

    def _act_open_search(self) -> None:
        if self.view != "search":
            self._show_view("search")
        search_input = self.query_one("#search", Input)
        search_input.focus()

    def _act_view_queue(self) -> None:
        self._show_view("queue")
        self.query_one("#list").focus()

    def _act_view_library(self) -> None:
        self._scan_library()
        self._show_view("library")
        self.query_one("#list").focus()

    def _act_overlay_close(self) -> None:
        if isinstance(self.focused, Input):
            self.focused.blur()
            self.query_one("#list").focus()

    def _act_toggle_rail(self) -> None:
        self.rail_visible = not self.rail_visible
        self.query_one(Rail).display = self.rail_visible

    def _act_toggle_downloads(self) -> None:
        panel = self.query_one(DownloadsPanel)
        self.downloads_visible = not self.downloads_visible
        panel.display = self.downloads_visible
        if self.downloads_visible:
            self._dl_dirty = True
            self._paint_downloads(time.monotonic())
            panel.focus()
        else:
            self.query_one("#list").focus()

    def _act_open_url(self) -> None:
        def answered(result: str | None) -> None:
            if result:
                self._play_source(result)

        self.push_screen(
            PromptScreen(
                "Open a video URL or a local file path.",
                title="Open",
                input_label="https://... or /path/to/file",
                theme_colors=self.ambient.colors,
            ),
            answered,
        )

    def _act_debug_overlay(self) -> None:
        snapshot = self.engine.snapshot()
        text = (
            f"state={snapshot.state.value} dev={snapshot.device_status} "
            f"buf={snapshot.buffering} prefetch={snapshot.prefetching} "
            f"queue={len(snapshot.queue)} idx={snapshot.index} "
            f"view={self.view} mode={self.query_one(VisualizerWidget).mode_name}"
        )
        self.query_one(StatusBar).set_message(text, ttl=8.0, kind="dim")

    def _act_plain_snapshot(self) -> None:
        snapshot = self.engine.snapshot()
        title = snapshot.track.title if snapshot.track else "-"
        text = (
            f"{snapshot.state.value}: {title[:36]} "
            f"{format_duration(snapshot.position)}/{format_duration(snapshot.duration)} "
            f"vol={round(snapshot.volume * 100)}% speed={snapshot.speed:g}x "
            f"shuffle={'on' if snapshot.shuffle else 'off'} repeat={snapshot.repeat}"
        )
        self.query_one(StatusBar).set_message(text, ttl=6.0, kind="accent")

    # -- transport -------------------------------------------------------
    def _act_play_pause(self) -> None:
        snapshot = self.engine.snapshot()
        if snapshot.state.value == "error" and snapshot.can_retry:
            self.engine.retry()
            return
        if not snapshot.queue:
            self.query_one(StatusBar).set_message(
                "Nothing queued. Search first, then press enter.", ttl=5.0
            )
            return
        if snapshot.state.value in ("idle", "stopped", "ended"):
            self.engine.play(snapshot.index, start_at=self._consume_start())
            return
        self.engine.toggle_pause()

    def _act_next_track(self) -> None:
        self.engine.next()

    def _act_prev_track(self) -> None:
        self.engine.prev()

    def _act_retry(self) -> None:
        if self.engine.snapshot().can_retry:
            self.engine.retry()
        else:
            self.query_one(StatusBar).set_message("Nothing to retry.", ttl=3.0)

    def _act_seek_back(self) -> None:
        self.engine.seek_relative(-5)

    def _act_seek_fwd(self) -> None:
        self.engine.seek_relative(5)

    def _act_seek_back_big(self) -> None:
        self.engine.seek_relative(-30)

    def _act_seek_fwd_big(self) -> None:
        self.engine.seek_relative(30)

    def _act_seek_back_pct(self) -> None:
        duration = self.engine.snapshot().duration
        self.engine.seek_relative(-(duration * 0.01) if duration else -5)

    def _act_seek_fwd_pct(self) -> None:
        duration = self.engine.snapshot().duration
        self.engine.seek_relative(duration * 0.01 if duration else 5)

    def _act_volume_up(self) -> None:
        value = self.engine.adjust_volume(0.05)
        self.query_one(StatusBar).set_message(
            f"Volume {round(value * 100)}%", ttl=1.5, kind="accent"
        )

    def _act_volume_down(self) -> None:
        value = self.engine.adjust_volume(-0.05)
        self.query_one(StatusBar).set_message(
            f"Volume {round(value * 100)}%", ttl=1.5, kind="accent"
        )

    def _act_mute(self) -> None:
        muted = self.engine.toggle_mute()
        self.query_one(StatusBar).set_message(
            "Muted." if muted else "Unmuted.", ttl=2.0, kind="accent"
        )

    def _act_shuffle(self) -> None:
        enabled = self.engine.toggle_shuffle()
        self.query_one(StatusBar).set_message(
            f"Shuffle {'on' if enabled else 'off'}.", ttl=2.0, kind="accent"
        )

    def _act_repeat(self) -> None:
        mode = self.engine.cycle_repeat()
        label = {"off": "off", "queue": "queue", "one": "one track"}[mode]
        self.query_one(StatusBar).set_message(f"Repeat: {label}.", ttl=2.0, kind="accent")

    def _act_speed_down(self) -> None:
        value = self.engine.set_speed(self.engine.snapshot().speed - 0.05)
        self.query_one(StatusBar).set_message(f"Speed {value:g}x", ttl=1.5, kind="accent")

    def _act_speed_up(self) -> None:
        value = self.engine.set_speed(self.engine.snapshot().speed + 0.05)
        self.query_one(StatusBar).set_message(f"Speed {value:g}x", ttl=1.5, kind="accent")

    def _act_equalizer(self) -> None:
        names = list(PRESETS)

        def answered(result: str | None) -> None:
            if not result:
                return
            self._eq_gains = list(preset_gains(result))
            self.engine.set_eq(self._eq_gains, result)
            self.query_one(StatusBar).set_message(
                f"EQ: {result}", ttl=3.0, kind="accent"
            )

        self.push_screen(
            PickerScreen("Equalizer", names, theme_colors=self.ambient.colors), answered
        )

    def _act_viz_cycle(self) -> None:
        name = self.query_one(VisualizerWidget).cycle_mode()
        self.query_one(StatusBar).set_message(f"Visualizer: {name}", ttl=2.5, kind="accent")

    # -- list rows -------------------------------------------------------
    def _act_row_queue(self) -> None:
        row = self.query_one(TrackList).selected_row
        if row is None:
            return
        self.engine.queue_insert([row.track])
        self.query_one(StatusBar).set_message(
            f"Queued {row.track.title[:50]}", ttl=3.0, kind="accent"
        )
        if self.view == "queue":
            self._refresh_rows()

    def _act_row_playlist(self) -> None:
        row = self.query_one(TrackList).selected_row
        if row is None:
            return
        options = [playlist.name for playlist in self.playlists.all()]
        options.append("+ New playlist")

        def chosen(result: str | None) -> None:
            if result is None:
                return
            if result == "+ New playlist":
                self._prompt_new_playlist(first_track=row.track)
                return
            playlist = self.playlists.by_name(result)
            if playlist is not None:
                self.playlists.add_track(playlist.id, row.track)
                self.query_one(StatusBar).set_message(
                    f"Added to {playlist.name}", ttl=3.0, kind="ok"
                )
                self._refresh_rail()

        self.push_screen(
            PickerScreen("Add to playlist", options, theme_colors=self.ambient.colors), chosen
        )

    def _act_row_download(self) -> None:
        row = self.query_one(TrackList).selected_row
        if row is None:
            return
        if "://" not in row.track.source and not row.track.source.startswith("file://"):
            if Path(row.track.source).expanduser().exists():
                self.query_one(StatusBar).set_message(
                    "That file is already local.", ttl=3.0
                )
                return
        self.downloads.add([row.track])
        if not self.downloads_visible:
            self._act_toggle_downloads()
        self.query_one(StatusBar).set_message(
            f"Downloading {row.track.title[:50]}", ttl=3.0, kind="accent"
        )

    def _act_row_move_up(self) -> None:
        self._move_row(-1)

    def _act_row_move_down(self) -> None:
        self._move_row(+1)

    def _move_row(self, delta: int) -> None:
        list_widget = self.query_one(TrackList)
        index = list_widget.highlighted
        if index is None:
            return
        if self.view == "queue":
            if self.engine.queue_move(index, delta):
                self._refresh_rows()
                list_widget.select_index(index + delta)
            return
        if self.view == "playlist" and self.playlist_id:
            if self.playlists.move_track(self.playlist_id, index, delta):
                self._refresh_rows()
                list_widget.select_index(index + delta)
            return
        self.query_one(StatusBar).set_message(
            "Reordering works in Queue and playlists.", ttl=3.0
        )

    # -- playlists -------------------------------------------------------
    def _act_playlist_new(self) -> None:
        self._prompt_new_playlist()

    def _prompt_new_playlist(self, first_track: Track | None = None) -> None:
        def answered(result: str | None) -> None:
            if not result:
                return
            playlist = self.playlists.create(result)
            if first_track is not None:
                self.playlists.add_track(playlist.id, first_track)
            self._refresh_rail()
            self.query_one(StatusBar).set_message(
                f"Created {playlist.name}", ttl=3.0, kind="ok"
            )

        self.push_screen(
            PromptScreen(
                "Name the new playlist.",
                title="New playlist",
                input_label="Playlist name",
                theme_colors=self.ambient.colors,
            ),
            answered,
        )

    def _act_playlist_rename(self) -> None:
        if not self.playlist_id:
            self.query_one(StatusBar).set_message(
                "Open a playlist first, then press R.", ttl=4.0
            )
            return
        current = self.playlist_name

        def answered(result: str | None) -> None:
            if not result or not self.playlist_id:
                return
            renamed = self.playlists.rename(self.playlist_id, result)
            if renamed:
                self.playlist_name = renamed.name
                self._refresh_rail()
                self._refresh_rows()
                self.query_one(StatusBar).set_message(
                    f"Renamed to {renamed.name}", ttl=3.0, kind="ok"
                )

        self.push_screen(
            PromptScreen(
                f"Rename \"{current}\".",
                title="Rename playlist",
                input_label="New name",
                initial=current,
                theme_colors=self.ambient.colors,
            ),
            answered,
        )

    def _act_playlist_delete(self) -> None:
        if not self.playlist_id:
            self.query_one(StatusBar).set_message(
                "Open a playlist first, then press delete.", ttl=4.0
            )
            return
        name = self.playlist_name
        playlist_id = self.playlist_id

        def answered(result: str | None) -> None:
            if result != "y":
                self.query_one(StatusBar).set_message("Not deleted.", ttl=2.0)
                return
            if self.playlists.delete(playlist_id):
                self.playlist_id = None
                self.playlist_name = ""
                self._refresh_rail()
                self._show_view("queue" if self.engine.snapshot().queue else "search")
                self.query_one(StatusBar).set_message(
                    f"Deleted {name}.", ttl=3.0, kind="ok"
                )

        self.push_screen(
            PromptScreen(
                f"Delete \"{name}\" and its {len(self._playlist_rows()[0])} tracks? "
                "This cannot be undone.",
                title="Delete playlist",
                choices=[("y", "Delete"), ("n", "Keep")],
                theme_colors=self.ambient.colors,
            ),
            answered,
        )

    def _act_playlist_import(self) -> None:
        def answered(result: str | None) -> None:
            if not result:
                return
            try:
                playlist = self.playlists.import_m3u(Path(result).expanduser())
            except Exception as exc:
                self.query_one(StatusBar).set_message(
                    f"Import failed: {first_error_line(str(exc))[:100]}",
                    ttl=6.0,
                    kind="danger",
                )
                return
            self._refresh_rail()
            self.query_one(StatusBar).set_message(
                f"Imported {playlist.name} ({len(playlist.tracks)} tracks).",
                ttl=4.0,
                kind="ok",
            )

        self.push_screen(
            PromptScreen(
                "Path to an .m3u or .m3u8 file.",
                title="Import playlist",
                input_label="/path/to/list.m3u",
                theme_colors=self.ambient.colors,
            ),
            answered,
        )

    def _act_playlist_export(self) -> None:
        if not self.playlist_id:
            self.query_one(StatusBar).set_message(
                "Open a playlist first, then press E.", ttl=4.0
            )
            return
        playlist = self.playlists.get(self.playlist_id)
        if playlist is None:
            return
        default = f"{playlist.name}.m3u"

        def answered(result: str | None) -> None:
            if not result or not self.playlist_id:
                return
            try:
                target = self.playlists.export_m3u(
                    self.playlist_id, Path(result).expanduser()
                )
            except Exception as exc:
                self.query_one(StatusBar).set_message(
                    f"Export failed: {first_error_line(str(exc))[:100]}",
                    ttl=6.0,
                    kind="danger",
                )
                return
            self.query_one(StatusBar).set_message(
                f"Wrote {target}", ttl=4.0, kind="ok"
            )

        self.push_screen(
            PromptScreen(
                f"Export \"{playlist.name}\" to an .m3u file.",
                title="Export playlist",
                input_label="Destination path",
                initial=str(Path.cwd() / default),
                theme_colors=self.ambient.colors,
            ),
            answered,
        )

    # -- rail ------------------------------------------------------------
    def _act_playlist_shuffle_play(self) -> None:
        entry_id = self.query_one(Rail).selected_playlist_id
        if not entry_id:
            return
        playlist = self.playlists.get(entry_id)
        if playlist is None or not playlist.tracks:
            return
        self.engine.set_shuffle(True)
        self._open_playlist(entry_id, play=True)

    def _act_playlist_inspect(self) -> None:
        entry_id = self.query_one(Rail).selected_playlist_id
        if entry_id:
            self._open_playlist(entry_id, play=False)

    def _open_playlist(self, playlist_id: str, *, play: bool) -> None:
        playlist = self.playlists.get(playlist_id)
        if playlist is None:
            return
        self.playlist_id = playlist.id
        self.playlist_name = playlist.name
        self._show_view("playlist")
        self.query_one(Rail).highlight_playlist(playlist.id)
        if play:
            if not playlist.tracks:
                self.query_one(StatusBar).set_message(
                    f"{playlist.name} is empty.", ttl=3.0
                )
                return
            self.engine.set_queue(list(playlist.tracks), 0)
            self.engine.play(0)
            self.query_one(StatusBar).set_message(
                f"Playing {playlist.name}", ttl=3.0, kind="accent"
            )

    # -- downloads panel actions ----------------------------------------
    def _act_download_retry(self) -> None:
        item_id = self.query_one(DownloadsPanel).selected_item_id
        if item_id and self.downloads.retry(item_id):
            self.query_one(StatusBar).set_message("Retrying download.", ttl=2.0)

    def _act_download_cancel(self) -> None:
        item_id = self.query_one(DownloadsPanel).selected_item_id
        if item_id and self.downloads.cancel(item_id):
            self.query_one(StatusBar).set_message("Canceling download.", ttl=2.0)

    # ==================================================================
    # OptionSelected wiring (enter / click)
    # ==================================================================
    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        widget = event.option_list
        if isinstance(widget, TrackList):
            event.stop()
            self._play_row(event.option_index)
            return
        if isinstance(widget, Rail):
            event.stop()
            entry_id = widget.selected_playlist_id
            if entry_id:
                self._open_playlist(entry_id, play=True)
            return
        # downloads panel: selection is visual only
        event.stop()

    # ==================================================================
    # Input wiring
    # ==================================================================
    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "search":
            return
        event.stop()
        self._submit_search(event.value)
        self.query_one("#list").focus()

    def _history_move(self, delta: int) -> None:
        entries = self.history.entries
        if not entries:
            return
        search_input = self.query_one("#search", Input)
        if self._history_pos is None:
            self._history_pos = len(entries)
        self._history_pos = max(0, min(len(entries), self._history_pos + delta))
        if self._history_pos >= len(entries):
            search_input.value = ""
        else:
            search_input.value = entries[self._history_pos]
        search_input.cursor_position = len(search_input.value)

    def _act_history_prev(self) -> None:
        if not isinstance(self.focused, Input) or self.focused.id != "search":
            return
        self._history_move(-1)

    def _act_history_next(self) -> None:
        if not isinstance(self.focused, Input) or self.focused.id != "search":
            return
        self._history_move(+1)

    # ==================================================================
    # Widgets: list heads, counts, downloads
    # ==================================================================
    def _paint_counts(self, snapshot: Any) -> None:
        if self.view == "search":
            base = f"{len(self.search_tracks)} results"
        elif self.view == "queue":
            base = f"{len(snapshot.queue)} queued"
        elif self.view == "library":
            base = f"{len(self.library_tracks)} files"
        else:
            base = f"{self.playlist_name or 'playlist'}"
        active = sum(
            1
            for item in self.downloads.snapshot()
            if item.state in (DownloadState.QUEUED, DownloadState.DOWNLOADING, DownloadState.CONVERTING)
        )
        if active:
            base = f"{base}  {active} downloading"
        self.query_one(StatusBar).set_counts(base)

    def _paint_downloads(self, now: float) -> None:
        if not self.downloads_visible and not self._dl_dirty:
            return
        self._dl_dirty = False
        panel = self.query_one(DownloadsPanel)
        panel.set_items(self.downloads.snapshot())
        if self.downloads.needs_exists_answer and not self._exists_open:
            self._ask_exists()

    def _ask_exists(self) -> None:
        item = self.downloads.exists_question
        if item is None:
            return
        self._exists_open = True
        name = item.target or item.title

        def answered(result: str | None) -> None:
            self._exists_open = False
            if result is None or result == "s":
                self.downloads.resolve_exists("skip")
            elif result == "o":
                self.downloads.resolve_exists("overwrite")
            elif result == "a":
                self.downloads.resolve_exists("overwrite_all")
            elif result == "k":
                self.downloads.resolve_exists("skip_all")
            elif result == "x":
                self.downloads.cancel(item.id)
                self.downloads.resolve_exists("skip")

        self.push_screen(
            PromptScreen(
                f"\"{Path(str(name)).name}\" already exists in the destination folder.",
                title="File exists",
                choices=[
                    ("o", "Overwrite this one"),
                    ("s", "Skip this one"),
                    ("a", "Overwrite all"),
                    ("k", "Skip all"),
                    ("x", "Cancel download"),
                ],
                theme_colors=self.ambient.colors,
            ),
            answered,
        )

    def _download_event(self, kind: str, item: Any) -> None:
        """Called from download worker threads - flag only, paint in tick."""
        self._dl_dirty = True
        if kind == "changed" and item.state == DownloadState.DONE:
            log.debug("download finished: %s", item.title)

    # ==================================================================
    # Rail
    # ==================================================================
    def _refresh_rail(self) -> None:
        entries = [
            RailEntry(playlist.id, playlist.name, len(playlist.tracks))
            for playlist in self.playlists.all()
        ]
        rail = self.query_one(Rail)
        rail.set_entries(entries)
        rail.display = self.rail_visible
        if self.playlist_id:
            rail.highlight_playlist(self.playlist_id)

    # ==================================================================
    # Now playing paint
    # ==================================================================
    def _paint_nowplaying(self, snapshot: Any) -> None:
        widget = self.query_one(NowPlaying)
        widget.show_state(
            position=snapshot.position,
            duration=snapshot.duration,
            volume=snapshot.volume,
            muted=snapshot.muted,
            shuffle=snapshot.shuffle,
            repeat=snapshot.repeat,
            speed=snapshot.speed,
            eq_preset=snapshot.eq_preset,
            state=snapshot.state.value,
            breath=self._breath,
            motion=self.ambient.motion,
        )

    # ==================================================================
    # Session
    # ==================================================================
    def _restore_session(self) -> None:
        if not getattr(self.config, "restore_session", True):
            return
        state = self.session_store.load()
        if state is None:
            return
        if state.view in ("search", "queue", "library"):
            self.view = state.view
        elif state.view == "playlist":
            self.view = "queue" if state.queue else "search"
        self.rail_visible = state.rail_visible
        visualizer = self.query_one(VisualizerWidget)
        visualizer.set_mode(state.visualizer)
        engine = self.engine
        engine.set_volume(state.volume)
        if state.muted and not engine.muted:
            engine.toggle_mute()
        engine.set_shuffle(state.shuffle)
        for _ in range(3):
            if engine.repeat == state.repeat:
                break
            engine.cycle_repeat()
        engine.set_speed(state.speed)
        if state.eq_preset != "flat":
            self._eq_gains = list(state.eq_gains)
            engine.set_eq(self._eq_gains, state.eq_preset)
        tracks: list[Track] = []
        for item in state.queue:
            track = Track.from_dict(item)
            if track is not None:
                tracks.append(track)
        if tracks:
            engine.set_queue(tracks, state.index)
            if state.position > 1.0:
                self._pending_start = state.position

    def _save_session(self) -> None:
        try:
            snapshot = self.engine.snapshot()
            state = SessionState(
                track=snapshot.track.to_dict() if snapshot.track else None,
                position=self.engine.position() if snapshot.track else 0.0,
                queue=[track.to_dict() for track in snapshot.queue],
                index=snapshot.index,
                volume=snapshot.volume,
                muted=snapshot.muted,
                shuffle=snapshot.shuffle,
                repeat=snapshot.repeat,
                speed=snapshot.speed,
                eq_preset=snapshot.eq_preset,
                eq_gains=list(self._eq_gains),
                visualizer=self.query_one(VisualizerWidget).mode_name,
                rail_visible=self.rail_visible,
                view=self.view,
            )
            self.session_store.save(state)
        except Exception as exc:  # pragma: no cover - never block exit on save
            log.warning("session save failed: %s", exc)

    # ==================================================================
    # Threads and shutdown
    # ==================================================================
    def _spawn(self, name: str, work: Callable[[], None]) -> None:
        def runner() -> None:
            try:
                work()
            except Exception as exc:  # pragma: no cover - defensive
                log.error("background job %s failed: %s", name, exc)

        threading.Thread(target=runner, name=f"tide-{name}", daemon=True).start()

    def shutdown(self) -> None:
        """Persist state and stop every worker (idempotent, any exit path)."""
        if self._closing:
            return
        self._closing = True
        self._save_session()
        try:
            self.downloads.close()
        except Exception:  # pragma: no cover
            pass
        try:
            self.engine.close(save_state=False)
        except Exception:  # pragma: no cover
            pass


__all__ = ["TideApp"]
