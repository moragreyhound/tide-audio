"""Wash: slow soft blobs driven by the 16 low-resolution wash bands."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from tide.visual.modes.base import VisualizerMode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tide.audio.analysis import AnalysisFrame
    from tide.visual.braille import BrailleCanvas
    from tide.visual.palette import Palette

BLOB_SPACING = 6  # dots between blob centers


class WashMode(VisualizerMode):
    name = "wash"
    label = "Wash"

    def draw_idle(self, canvas: "BrailleCanvas", palette: "Palette | None") -> None:
        # Even dim field at rest: sparse checker, static.
        for y in range(0, canvas.dot_height, 4):
            for x in range(0, canvas.dot_width, 4):
                canvas.set_dot(x, y, True)

    def draw(self, canvas, frame: "AnalysisFrame", palette) -> None:
        dot_w, dot_h = canvas.dot_width, canvas.dot_height
        wash = self.aggregate(frame.wash, 16)
        phase = self.tick * 0.05

        # blob centers on a jittered grid, breathing with the wash bands
        cols = max(2, dot_w // BLOB_SPACING)
        rows = max(2, dot_h // BLOB_SPACING)
        cells = cols * rows
        values = self.aggregate(wash, cells)
        index = 0
        for row in range(rows):
            for col in range(cols):
                value = values[index] if index < len(values) else 0.0
                index += 1
                jitter_x = math.sin(phase + index * 1.7) * 1.5
                jitter_y = math.cos(phase * 0.8 + index * 2.3) * 1.5
                cx = col * BLOB_SPACING + BLOB_SPACING / 2 + jitter_x
                cy = row * BLOB_SPACING + BLOB_SPACING / 2 + jitter_y
                radius = 0.8 + value * 3.4
                if radius < 1.0:
                    continue
                r2 = radius * radius
                x0, x1 = int(cx - radius) - 1, int(cx + radius) + 2
                y0, y1 = int(cy - radius) - 1, int(cy + radius) + 2
                for y in range(max(0, y0), min(dot_h, y1)):
                    for x in range(max(0, x0), min(dot_w, x1)):
                        dx, dy = x - cx, y - cy
                        dist2 = dx * dx + dy * dy
                        if dist2 <= r2:
                            # dither the rim so blobs read as soft, not solid
                            edge = dist2 / r2
                            if edge < 0.65 or (x + y + self.tick) % 2 == 0:
                                canvas.set_dot(x, y, True)
