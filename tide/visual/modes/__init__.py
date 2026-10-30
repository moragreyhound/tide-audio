"""Visualizer mode registry: ``mode_name -> class`` with a safe factory."""

from __future__ import annotations

import random

from tide.visual.modes.base import VisualizerMode
from tide.visual.modes.particles import ParticlesMode
from tide.visual.modes.radial import RadialMode
from tide.visual.modes.scope import ScopeMode
from tide.visual.modes.spectrum import SpectrumMode
from tide.visual.modes.wash import WashMode

MODES: dict[str, type[VisualizerMode]] = {
    SpectrumMode.name: SpectrumMode,
    ScopeMode.name: ScopeMode,
    RadialMode.name: RadialMode,
    ParticlesMode.name: ParticlesMode,
    WashMode.name: WashMode,
}

DEFAULT_MODE = SpectrumMode.name


def mode_names() -> list[str]:
    """Ordered mode names as they cycle with ``v``."""
    return list(MODES)


def next_mode(current: str) -> str:
    names = mode_names()
    try:
        index = names.index(current)
    except ValueError:
        return names[0]
    return names[(index + 1) % len(names)]


def create_mode(name: str, *, rng: random.Random | None = None) -> VisualizerMode:
    """Instantiate ``name``; unknown names fall back to the spectrum."""
    mode_cls = MODES.get(name, MODES[DEFAULT_MODE])
    return mode_cls(rng)


__all__ = [
    "MODES",
    "DEFAULT_MODE",
    "VisualizerMode",
    "SpectrumMode",
    "ScopeMode",
    "RadialMode",
    "ParticlesMode",
    "WashMode",
    "mode_names",
    "next_mode",
    "create_mode",
]
