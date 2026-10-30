"""Particles: seeded drifting points spawned on beats."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tide.visual.modes.base import VisualizerMode

if TYPE_CHECKING:  # pragma: no cover - typing only
    from tide.audio.analysis import AnalysisFrame
    from tide.visual.braille import BrailleCanvas
    from tide.visual.palette import Palette

MAX_PARTICLES = 42
PARTICLE_LIFE = 46        # ticks
DRIFT = 0.055             # upward drift per tick
SPAWN_PER_BEAT = 5


@dataclass(slots=True)
class _Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: int


class ParticlesMode(VisualizerMode):
    name = "particles"
    label = "Particles"

    def __init__(self, rng=None) -> None:
        super().__init__(rng)
        self._particles: list[_Particle] = []

    def reset(self) -> None:
        super().reset()
        self._particles = []

    def draw_idle(self, canvas: "BrailleCanvas", palette: "Palette | None") -> None:
        # Resting: a few static dots in fixed positions (no motion at rest).
        dot_w, dot_h = canvas.dot_width, canvas.dot_height
        for i in range(7):
            x = int((i + 1) * dot_w / 8)
            y = dot_h - 1 if i % 2 == 0 else dot_h - 2
            canvas.set_dot(x, y, True)

    def draw(self, canvas, frame: "AnalysisFrame", palette) -> None:
        dot_w, dot_h = canvas.dot_width, canvas.dot_height
        cx, cy = dot_w / 2.0, dot_h / 2.0

        if frame.beat:
            count = SPAWN_PER_BEAT + int(min(1.0, frame.rms) * 4)
            for _ in range(count):
                if len(self._particles) >= MAX_PARTICLES:
                    self._particles.pop(0)
                angle = self.rng.uniform(0, 2 * math.pi)
                speed = self.rng.uniform(0.15, 0.55) + frame.rms * 0.4
                self._particles.append(
                    _Particle(
                        x=cx,
                        y=cy,
                        vx=math.cos(angle) * speed,
                        vy=math.sin(angle) * speed,
                        life=PARTICLE_LIFE,
                    )
                )

        alive: list[_Particle] = []
        for particle in self._particles:
            particle.x += particle.vx
            particle.y += particle.vy - DRIFT
            particle.vx *= 0.985
            particle.vy *= 0.985
            particle.life -= 1
            if particle.life <= 0:
                continue
            if -2 <= particle.x < dot_w + 2 and -2 <= particle.y < dot_h + 2:
                alive.append(particle)
        self._particles = alive

        for particle in self._particles:
            x, y = int(particle.x), int(particle.y)
            canvas.set_dot(x, y, True)
            # older particles leave a single trailing dot behind them
            if particle.life < PARTICLE_LIFE - 3:
                canvas.set_dot(int(x - particle.vx), int(y - particle.vy), True)
