"""10-band equalizer: presets, gain validation, FFmpeg filter specs.

Gains are dB values in ``[-12, +12]`` for 10 log-spaced bands from 31 Hz to
16 kHz. The decoder turns non-flat bands into ffmpeg ``equalizer`` (peaking
biquad) filters, so the DSP runs in C and the Python side stays a pure
configuration layer - trivially testable.
"""

from __future__ import annotations

from typing import Sequence

BAND_FREQUENCIES: tuple[int, ...] = (31, 62, 125, 250, 500, 1000, 2000, 4000, 8000, 16000)

GAIN_MIN = -12.0
GAIN_MAX = 12.0

PRESETS: dict[str, tuple[float, ...]] = {
    "flat": (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0),
    "bass boost": (7.0, 6.0, 4.5, 2.5, 1.0, 0.0, -0.5, -0.5, 0.0, 0.0),
    "vocal": (-2.0, -2.5, -1.0, 1.5, 3.5, 4.5, 3.5, 2.0, 0.5, -0.5),
    "late night": (3.0, 3.5, 2.0, 0.5, -0.5, -1.0, -1.5, -2.5, -4.0, -6.0),
}

PRESET_ORDER: tuple[str, ...] = ("flat", "bass boost", "vocal", "late night")


def clamp_gain(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(GAIN_MIN, min(GAIN_MAX, number))


def validate_gains(gains: Sequence[float] | None) -> list[float]:
    """Return exactly 10 clamped floats; anything malformed becomes 0.0."""
    out: list[float] = []
    for index in range(10):
        if gains is not None and index < len(gains):
            try:
                value = float(gains[index])
            except (TypeError, ValueError):
                value = 0.0
            if value != value:  # NaN
                value = 0.0
            out.append(clamp_gain(value))
        else:
            out.append(0.0)
    return out


def preset_gains(name: str) -> list[float]:
    """Gains for a preset name; unknown names fall back to flat."""
    return list(PRESETS.get(name.strip().lower(), PRESETS["flat"]))


def next_preset(current: str) -> str:
    """Cycle flat -> bass boost -> vocal -> late night -> flat."""
    try:
        index = PRESET_ORDER.index(current.strip().lower())
    except ValueError:
        return PRESET_ORDER[0]
    return PRESET_ORDER[(index + 1) % len(PRESET_ORDER)]


def is_flat(gains: Sequence[float]) -> bool:
    return all(abs(g) < 0.5 for g in gains)


def describe(gains: Sequence[float]) -> str:
    """One-line summary for the status bar, e.g. ``bass +7 · treble -6``."""
    if is_flat(gains):
        return "flat"
    clamped = validate_gains(gains)
    bass = max(clamped[0:3])
    treble = min(clamped[7:10])
    parts = []
    if bass >= 0.5:
        parts.append(f"bass +{bass:g}")
    if treble <= -0.5:
        parts.append(f"treble {treble:g}")
    if not parts:
        mid = max(clamped[3:7], key=abs)
        if abs(mid) >= 0.5:
            parts.append(f"mid {mid:+g}")
    return " · ".join(parts) if parts else "custom"
