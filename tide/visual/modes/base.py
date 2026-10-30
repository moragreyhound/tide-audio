"""Shared visualizer mode contract.

A mode draws dots into a :class:`~tide.visual.braille.BrailleCanvas` from an
:class:`~tide.audio.analysis.AnalysisFrame`. Modes own their animation state
(peak holds, particles, rings) and are deterministic when given a seeded RNG,
which is what the tests rely on.

Color is deliberately not applied here: the widget colors the finished dot
grid from the current palette, keeping modes monochrome and testable.
"""

from __future__ import annotations

import random
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tide.audio.analysis import AnalysisFrame
    from tide.visual.braille import BrailleCanvas
    from tide.visual.palette import Palette


class VisualizerMode:
    """Base class: routes idle vs. active frames and advances the tick."""

    name = "base"
    label = "Base"

    def __init__(self, rng: random.Random | None = None) -> None:
        self.rng = rng if rng is not None else random.Random(0)
        self.tick = 0

    def reset(self) -> None:
        """Clear per-track state (called on track change and mode switch)."""
        self.tick = 0

    # -- entry points ----------------------------------------------------
    def render(
        self,
        canvas: "BrailleCanvas",
        frame: "AnalysisFrame | None",
        palette: "Palette | None" = None,
    ) -> None:
        """Draw one frame. ``frame is None`` means nothing is playing."""
        canvas.clear()
        if canvas.width == 0 or canvas.height == 0:
            return
        if frame is None:
            self.draw_idle(canvas, palette)
            return
        self.draw(canvas, frame, palette)
        self.tick += 1

    def draw_idle(self, canvas: "BrailleCanvas", palette: "Palette | None") -> None:
        """Static resting state: nothing is playing, so nothing moves."""
        raise NotImplementedError

    def draw(
        self,
        canvas: "BrailleCanvas",
        frame: "AnalysisFrame",
        palette: "Palette | None",
    ) -> None:
        raise NotImplementedError

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def aggregate(values: tuple[float, ...] | list[float], count: int) -> list[float]:
        """Reduce ``values`` to ``count`` buckets by max (never misses peaks)."""
        n = len(values)
        if count <= 0:
            return []
        if n == 0:
            return [0.0] * count
        out: list[float] = []
        for i in range(count):
            start = int(i * n / count)
            end = max(start + 1, int((i + 1) * n / count))
            out.append(max(values[start:end]))
        return out
