"""Spectrum: bottom-anchored bars with decaying peak holds."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tide.visual.modes.base import VisualizerMode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tide.audio.analysis import AnalysisFrame
    from tide.visual.braille import BrailleCanvas
    from tide.visual.palette import Palette

PEAK_DECAY = 0.02  # peak dots per tick when the band is quiet


class SpectrumMode(VisualizerMode):
    name = "spectrum"
    label = "Spectrum"

    def __init__(self, rng=None) -> None:
        super().__init__(rng)
        self._peaks: list[float] = []

    def reset(self) -> None:
        super().reset()
        self._peaks = []

    def draw_idle(self, canvas: "BrailleCanvas", palette: "Palette | None") -> None:
        # One quiet baseline row: the instrument is at rest.
        y = canvas.dot_height - 1
        for x in range(canvas.dot_width):
            if x % 2 == 0:
                canvas.set_dot(x, y, True)

    def draw(self, canvas, frame: "AnalysisFrame", palette) -> None:
        dot_w, dot_h = canvas.dot_width, canvas.dot_height
        values = self.aggregate(frame.bands, dot_w)
        if len(self._peaks) != len(values):
            self._peaks = [0.0] * len(values)
        floor_y = dot_h - 1
        usable = max(1, dot_h - 1)
        for x, value in enumerate(values):
            level = max(0.0, min(1.0, value))
            height = int(level * usable)
            top = floor_y - height
            for y in range(top, floor_y + 1):
                canvas.set_dot(x, y, True)
            # peak hold: one dot above the current top, easing down
            peak = self._peaks[x]
            if level >= peak:
                peak = level
            else:
                peak = max(level, peak - PEAK_DECAY)
            self._peaks[x] = peak
            peak_y = floor_y - max(1, int(peak * usable))
            if peak_y < top:
                canvas.set_dot(x, peak_y, True)
