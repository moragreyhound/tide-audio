"""Scope: oscilloscope trace of the decimated waveform."""

from __future__ import annotations

from typing import TYPE_CHECKING

from tide.visual.modes.base import VisualizerMode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tide.audio.analysis import AnalysisFrame
    from tide.visual.braille import BrailleCanvas
    from tide.visual.palette import Palette


class ScopeMode(VisualizerMode):
    name = "scope"
    label = "Scope"

    def draw_idle(self, canvas: "BrailleCanvas", palette: "Palette | None") -> None:
        # Flat center line at rest.
        y = canvas.dot_height // 2
        for x in range(canvas.dot_width):
            canvas.set_dot(x, y, True)

    def draw(self, canvas, frame: "AnalysisFrame", palette) -> None:
        dot_w, dot_h = canvas.dot_width, canvas.dot_height
        wave = frame.wave
        points: list[tuple[int, int]] = []
        mid = (dot_h - 1) / 2.0
        amplitude = max(1.0, mid * 0.95)
        count = len(wave)
        if count < 2 or dot_w < 2:
            return
        for x in range(dot_w):
            position = x * (count - 1) / (dot_w - 1)
            index = int(position)
            frac = position - index
            sample = wave[index]
            if index + 1 < count:
                sample = sample + (wave[index + 1] - sample) * frac
            y = int(round(mid - sample * amplitude))
            points.append((x, max(0, min(dot_h - 1, y))))
        # connect consecutive points so the trace never gaps
        for (x0, y0), (x1, y1) in zip(points, points[1:]):
            if abs(x1 - x0) == 1 and abs(y1 - y0) <= 1:
                canvas.set_dot(x0, y0, True)
                canvas.set_dot(x1, y1, True)
            else:
                canvas.draw_line(x0, y0, x1, y1, True)
