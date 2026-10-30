"""Radial: bars radiating from the center with a beat-driven ring."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from tide.visual.modes.base import VisualizerMode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tide.audio.analysis import AnalysisFrame
    from tide.visual.braille import BrailleCanvas
    from tide.visual.palette import Palette

RING_DECAY = 0.06


class RadialMode(VisualizerMode):
    name = "radial"
    label = "Radial"

    def __init__(self, rng=None) -> None:
        super().__init__(rng)
        self._ring = 0.0

    def reset(self) -> None:
        super().reset()
        self._ring = 0.0

    def draw_idle(self, canvas: "BrailleCanvas", palette: "Palette | None") -> None:
        cx, cy = canvas.dot_width // 2, canvas.dot_height // 2
        # a small static diamond marks the center while at rest
        for dx, dy in ((0, -1), (-1, 0), (0, 1), (1, 0)):
            canvas.set_dot(cx + dx, cy + dy, True)

    def draw(self, canvas, frame: "AnalysisFrame", palette) -> None:
        dot_w, dot_h = canvas.dot_width, canvas.dot_height
        cx, cy = (dot_w - 1) / 2.0, (dot_h - 1) / 2.0
        max_r = min(cx, cy)
        if max_r < 3:
            # Too small for a ring: fall back to a simple center marker.
            self.draw_idle(canvas, palette)
            return
        spokes = max(12, min(len(frame.bands), int(max_r * 8)))
        values = self.aggregate(frame.bands, spokes)
        inner = max(2.0, max_r * 0.30)
        span = max_r - inner - 1

        # spokes
        for i, value in enumerate(values):
            angle = -math.pi / 2 + (2 * math.pi * i / spokes)
            length = inner + max(0.0, min(1.0, value)) * span
            x1 = int(round(cx + math.cos(angle) * length))
            y1 = int(round(cy + math.sin(angle) * length))
            x0 = int(round(cx + math.cos(angle) * inner))
            y0 = int(round(cy + math.sin(angle) * inner))
            canvas.draw_line(x0, y0, x1, y1, True)

        # beat ring: expands once per beat, fades as it travels out
        if frame.beat:
            self._ring = 1.0
        elif self._ring > 0.0:
            self._ring = max(0.0, self._ring - RING_DECAY)
        if self._ring > 0.05:
            radius = inner + (1.0 - self._ring) * span
            steps = max(16, int(radius * 6))
            for s in range(steps):
                angle = 2 * math.pi * s / steps
                x = int(round(cx + math.cos(angle) * radius))
                y = int(round(cy + math.sin(angle) * radius))
                canvas.set_dot(x, y, True)
