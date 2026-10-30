"""Ambient theming: eased transitions between artwork palettes.

When a new track's palette arrives, the UI must not snap: colors interpolate
in Lab space (perceptually even) over a short duration so the room changes
with the record. Reduced motion switches instantly; ``none`` and ``NO_COLOR``
behave the same way.

The ambient object also derives the semantic UI colors (text, dim text, faint
borders) from the current palette so every widget reads the same values.
"""

from __future__ import annotations

from dataclasses import dataclass

from tide.log import get_logger
from tide.visual.color import (
    RGB,
    contrast_ratio,
    ensure_contrast,
    lab_to_rgb,
    mix,
    rgb_to_lab,
    rgb_to_hex,
)
from tide.visual.palette import Palette

log = get_logger(__name__)

DEFAULT_DURATION = 0.9  # seconds for a full palette transition


@dataclass(frozen=True, slots=True)
class ThemeColors:
    """Semantic colors derived from the active palette (hex strings)."""

    background: str
    background_alt: str
    foreground: str
    dim: str
    faint: str
    accent: str
    accent_dim: str
    accent_foreground: str  # readable text placed on the accent
    border: str
    border_focus: str
    danger: str
    ok: str

    def to_dict(self) -> dict[str, str]:
        return {
            "background": self.background,
            "background_alt": self.background_alt,
            "foreground": self.foreground,
            "dim": self.dim,
            "faint": self.faint,
            "accent": self.accent,
            "accent_dim": self.accent_dim,
            "accent_foreground": self.accent_foreground,
            "border": self.border,
            "border_focus": self.border_focus,
            "danger": self.danger,
            "ok": self.ok,
        }


def derive_colors(palette: Palette) -> ThemeColors:
    """Compute readable semantic colors for one palette."""
    bg = palette.background
    fg = ensure_contrast(palette.background_light, bg, target=7.0)
    dim = mix(fg, bg, 0.55)
    faint = mix(fg, bg, 0.76)
    accent = ensure_contrast(palette.accent, bg, target=3.0)
    accent_dim = mix(accent, bg, 0.5)
    # text on an accent-filled button: pick black or white by contrast
    black = RGB(10, 10, 10)
    white = RGB(245, 245, 243)
    accent_fg = black if contrast_ratio(black, accent) >= contrast_ratio(white, accent) else white
    border = mix(dim, bg, 0.55)
    border_focus = accent
    danger = ensure_contrast(RGB(196, 96, 88), bg, target=3.0)
    ok = ensure_contrast(RGB(128, 168, 120), bg, target=3.0)
    return ThemeColors(
        background=rgb_to_hex(bg),
        background_alt=rgb_to_hex(mix(bg, fg, 0.06)),
        foreground=rgb_to_hex(fg),
        dim=rgb_to_hex(dim),
        faint=rgb_to_hex(faint),
        accent=rgb_to_hex(accent),
        accent_dim=rgb_to_hex(accent_dim),
        accent_foreground=rgb_to_hex(accent_fg),
        border=rgb_to_hex(border),
        border_focus=rgb_to_hex(border_focus),
        danger=rgb_to_hex(danger),
        ok=rgb_to_hex(ok),
    )


class AmbientTheme:
    """Holds the current palette and eases toward the target palette."""

    def __init__(
        self,
        palette: Palette | None = None,
        *,
        duration: float = DEFAULT_DURATION,
        motion: str = "full",
    ) -> None:
        start = palette or Palette.fallback()
        self._from: Palette = start
        self._current: Palette = start
        self._target: Palette = start
        self._t: float = 1.0  # finished
        self.duration = max(0.0, float(duration))
        self.motion = motion if motion in ("full", "reduced", "none") else "full"
        self._colors: ThemeColors = derive_colors(self._current)

    # -- state -----------------------------------------------------------
    @property
    def palette(self) -> Palette:
        return self._current

    @property
    def colors(self) -> ThemeColors:
        return self._colors

    @property
    def transitioning(self) -> bool:
        return self._t < 1.0

    @property
    def progress(self) -> float:
        return self._t

    def set_palette(self, palette: Palette | None, *, immediate: bool | None = None) -> None:
        """Target a new palette (no-op if identical)."""
        if palette is None or palette.source_hash == self._target.source_hash:
            if palette is not None and palette.source_hash == self._current.source_hash:
                return
            if palette is None:
                return
        snap = immediate if immediate is not None else (self.motion != "full" or self.duration <= 0)
        if snap:
            self._from = palette
            self._current = palette
            self._target = palette
            self._t = 1.0
            self._colors = derive_colors(palette)
            return
        self._from = self._current
        self._target = palette
        self._t = 0.0
        log.debug("ambient transition to %s", palette.source_hash[:8])

    def advance(self, dt: float) -> bool:
        """Advance the transition; returns True when colors changed."""
        if self._t >= 1.0:
            return False
        if self.duration <= 0:
            self._t = 1.0
        else:
            self._t = min(1.0, self._t + max(0.0, dt) / self.duration)
        eased = _ease_in_out(self._t)
        self._current = lerp_palette(self._from, self._target, eased)
        self._colors = derive_colors(self._current)
        if self._t >= 1.0:
            self._current = self._target
            self._colors = derive_colors(self._current)
        return True

    def force_settled(self) -> None:
        """Jump to the target (used when motion is none)."""
        if self._t < 1.0:
            self._t = 1.0
            self._current = self._target
            self._colors = derive_colors(self._current)


def _ease_in_out(t: float) -> float:
    """Smoothstep: gentle at both ends, no visible start/stop pop."""
    clamped = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return clamped * clamped * (3.0 - 2.0 * clamped)


def lerp_palette(a: Palette, b: Palette, t: float) -> Palette:
    """Interpolate two palettes channel-by-channel in Lab space."""
    if t <= 0:
        return a
    if t >= 1:
        return b

    def blend(x: RGB, y: RGB) -> RGB:
        return lab_to_rgb(
            (
                rgb_to_lab(x)[0] + (rgb_to_lab(y)[0] - rgb_to_lab(x)[0]) * t,
                rgb_to_lab(x)[1] + (rgb_to_lab(y)[1] - rgb_to_lab(x)[1]) * t,
                rgb_to_lab(x)[2] + (rgb_to_lab(y)[2] - rgb_to_lab(x)[2]) * t,
            )
        )

    count = max(len(a.colors), len(b.colors))
    colors: list[RGB] = []
    for i in range(count):
        x = a.colors[i % len(a.colors)]
        y = b.colors[i % len(b.colors)]
        colors.append(blend(x, y))
    return Palette(
        colors=tuple(colors),
        accent=blend(a.accent, b.accent),
        dominant=blend(a.dominant, b.dominant),
        background=blend(a.background, b.background),
        background_light=blend(a.background_light, b.background_light),
        source_hash=b.source_hash,
    )
