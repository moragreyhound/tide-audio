"""Braille visualizer widget: renders the active mode and colors it.

The widget owns the canvas size, the current mode, and the frame feed. Color
is a simple bottom-weighted ramp from background to accent (monochrome base,
accent carries the artwork color). Motion policy:

* ``full``  - render on every incoming analysis frame (~30 fps);
* ``reduced`` - render at most twice per second;
* ``none``  - render only when the mode, size, or track changes.
"""

from __future__ import annotations

from rich.text import Text
from textual.events import Resize
from textual.widgets import Static

from tide.audio.analysis import AnalysisFrame
from tide.visual.braille import BrailleCanvas
from tide.visual.color import RGB, hex_to_rgb, mix, rgb_to_hex
from tide.visual.modes import VisualizerMode, create_mode, mode_names, next_mode
from tide.visual.palette import Palette
from tide.visual.ambient import ThemeColors

REDUCED_INTERVAL = 0.5  # seconds between renders under reduced motion


class VisualizerWidget(Static):
    def __init__(
        self, mode_name: str = "spectrum", motion: str = "full", **kwargs
    ) -> None:
        super().__init__(**kwargs)
        self.mode: VisualizerMode = create_mode(mode_name)
        self.motion = motion if motion in ("full", "reduced", "none") else "full"
        self.canvas = BrailleCanvas(40, 8)
        self.frame: AnalysisFrame | None = None
        self._colors: ThemeColors | None = None
        self._palette: Palette | None = None
        self._last_render: float = -10.0
        self._dirty = True
        self._last_payload: str = ""

    # -- configuration ---------------------------------------------------
    @property
    def mode_name(self) -> str:
        return self.mode.name

    def set_mode(self, name: str) -> None:
        if name == self.mode.name:
            return
        self.mode = create_mode(name)
        self.mode.reset()
        self._dirty = True

    def cycle_mode(self) -> str:
        name = next_mode(self.mode.name)
        self.set_mode(name)
        return name

    def set_colors(self, colors: ThemeColors, palette: Palette | None = None) -> None:
        self._colors = colors
        if palette is not None:
            self._palette = palette
        self._dirty = True

    def on_track_change(self) -> None:
        """Reset animation state when the track changes."""
        self.mode.reset()
        self.frame = None
        self._dirty = True

    def on_resize(self, event: Resize) -> None:
        width = max(4, self.size.width)
        height = max(2, self.size.height)
        if (width, height) != (self.canvas.width, self.canvas.height):
            self.canvas.resize(width, height)
            self._dirty = True

    # -- feed ------------------------------------------------------------
    def feed(self, frame: AnalysisFrame | None, *, now: float) -> None:
        """Offer the newest analysis frame; the motion policy decides."""
        self.frame = frame
        if self.motion == "none":
            return
        if self.motion == "reduced" and (now - self._last_render) < REDUCED_INTERVAL:
            self._dirty = True
            return
        self._dirty = True

    def render_if_dirty(self, *, now: float) -> bool:
        """Redraw when dirty; returns True when pixels changed."""
        if not self._dirty:
            return False
        width = max(4, self.size.width or self.canvas.width)
        height = max(2, self.size.height or self.canvas.height)
        if (width, height) != (self.canvas.width, self.canvas.height):
            self.canvas.resize(width, height)
        self.mode.render(self.canvas, self.frame, self._palette)
        rows = self.canvas.rows()
        payload = "\n".join(rows)
        if payload == self._last_payload:
            self._dirty = False
            return False
        self._last_payload = payload
        self._last_render = now
        self._dirty = False
        self.update(self._paint(rows))
        return True

    # -- color -----------------------------------------------------------
    def _paint(self, rows: list[str]) -> Text:
        colors = self._colors
        if colors is None or self.frame is None:
            # resting state: faint flat tint, no motion implied
            style = colors.faint if colors else "dim"
            return Text("\n".join(rows), style=style)
        background = hex_to_rgb(colors.background)
        accent = hex_to_rgb(colors.accent)
        text = Text()
        total = max(1, len(rows))
        for index, row in enumerate(rows):
            # brighter toward the bottom: the bars sit on the floor
            t = 0.30 + 0.70 * ((index + 1) / total)
            color = rgb_to_hex(_mix_rgb(background, accent, t))
            text.append(row, style=color)
            if index + 1 < total:
                text.append("\n")
        return text


def _mix_rgb(a: RGB, b: RGB, t: float) -> RGB:
    return mix(a, b, t)


__all__ = ["VisualizerWidget", "mode_names"]
