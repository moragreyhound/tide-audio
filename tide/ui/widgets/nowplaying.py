"""Now playing: title line plus a fixed-width progress row.

All numbers are tabular so the bar never jitters as digits change. The
buffering indicator breathes (``. .. ...``) only under full motion; reduced
and none render the word alone.
"""

from __future__ import annotations

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from tide.store.playlists import Track
from tide.textutil import format_duration, tabular
from tide.visual.ambient import ThemeColors

BREATH_STEPS = ("", ".", "..", "...")


class NowPlaying(Vertical):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._colors: ThemeColors | None = None
        self._last_title: str | None = None

    def compose(self):
        yield Horizontal(Static("Nothing playing", id="np-title"), Static("", id="np-flags"), id="np-head")
        yield Horizontal(Static("", id="np-bar"), Static("", id="np-times"), id="np-row")

    def set_colors(self, colors: ThemeColors) -> None:
        if colors == self._colors:
            return
        self._colors = colors
        self._last_title = None

    def show_track(self, track: Track | None) -> None:
        colors = self._colors
        if track is None:
            text = Text("Nothing playing", style=colors.dim if colors else "dim")
        else:
            text = Text()
            title = track.title or track.source
            channel = track.channel
            text.append(title, style=f"bold {colors.foreground}" if colors else "bold")
            if channel:
                text.append("  ", style=colors.dim if colors else "dim")
                text.append(channel, style=colors.dim if colors else "dim")
        payload = text.plain
        if payload != self._last_title:
            self._last_title = payload
            self.query_one("#np-title", Static).update(text)

    def show_state(
        self,
        *,
        position: float,
        duration: int | None,
        volume: float,
        muted: bool,
        shuffle: bool,
        repeat: str,
        speed: float,
        eq_preset: str,
        state: str,
        breath: int = 0,
        motion: str = "full",
    ) -> None:
        colors = self._colors
        dim = colors.dim if colors else "dim"
        foreground = colors.foreground if colors else ""
        accent = colors.accent if colors else ""
        faint = colors.faint if colors else "dim"

        # -- flags (right of the title) ----------------------------------
        flags: list[tuple[str, str]] = []
        if state == "buffering":
            dots = BREATH_STEPS[breath % 4] if motion == "full" else ""
            flags.append((f"buffering{dots}", accent))
        elif state in ("paused", "seeking", "error", "ended"):
            flags.append((state, accent if state == "error" else dim))
        elif state == "playing":
            flags.append(("playing", dim))
        if muted:
            flags.append(("muted", accent))
        if shuffle:
            flags.append(("shuffle", dim))
        if repeat != "off":
            flags.append((f"repeat:{repeat}", dim))
        if abs(speed - 1.0) > 1e-6:
            flags.append((f"{speed:.2g}x", dim))
        if eq_preset and eq_preset != "flat":
            flags.append((f"eq:{eq_preset}", dim))
        volume_text = "vol --" if muted else f"vol {round(volume * 100):>3d}%"
        flags.append((volume_text, faint))

        flag_text = Text()
        for index, (label, style) in enumerate(flags):
            if index:
                flag_text.append("  ")
            flag_text.append(label, style=style or "")
        self.query_one("#np-flags", Static).update(flag_text)

        # -- progress bar + times ----------------------------------------
        width = self.query_one("#np-bar", Static).size.width or 40
        width = max(12, width)
        time_text = f"{format_duration(position)} / {format_duration(duration)}"
        bar_width = max(6, width - len(time_text) - 3)
        if duration:
            fraction = max(0.0, min(1.0, position / duration))
            fill = int(fraction * bar_width)
        else:
            fill = 0
        bar = Text()
        if duration:
            bar.append("━" * fill, style=f"bold {accent}" if accent else "bold")
            bar.append("─" * (bar_width - fill), style=faint)
        else:
            bar.append("─" * bar_width, style=faint)
        self.query_one("#np-bar", Static).update(bar)
        self.query_one("#np-times", Static).update(Text(time_text, style=dim))
