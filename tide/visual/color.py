"""Color conversions and contrast math used by palette extraction and theming.

Pure NumPy/stdlib - no Pillow, no UI imports. All conversions follow the
standard definitions:

* sRGB <-> linear RGB uses the IEC 61966-2-1 transfer function.
* linear RGB <-> XYZ uses the D65 matrix; XYZ <-> Lab uses the CIE 1976
  formulas with the D65 white point (Xn=0.95047, Yn=1.0, Zn=1.08883).
* Contrast follows the WCAG 2.x relative-luminance ratio.
"""

from __future__ import annotations

import math

from typing import NamedTuple


class RGB(NamedTuple):
    """An sRGB triple (0-255). Behaves as a plain 3-tuple."""

    r: int
    g: int
    b: int


Lab = tuple[float, float, float]

_WHITE = (0.95047, 1.00000, 1.08883)
_EPS = 216.0 / 24389.0
_KAPPA = 24389.0 / 27.0

_M_RGB_TO_XYZ = (
    (0.4124564, 0.3575761, 0.1804375),
    (0.2126729, 0.7151522, 0.0721750),
    (0.0193339, 0.1191920, 0.9503041),
)
_M_XYZ_TO_RGB = (
    (3.2404542, -1.5371385, -0.4985314),
    (-0.9692660, 1.8760108, 0.0415560),
    (0.0556434, -0.2040259, 1.0572252),
)


# ---------------------------------------------------------------------------
# SRGB <-> linear
# ---------------------------------------------------------------------------
def _srgb_to_linear_channel(c: float) -> float:
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _linear_to_srgb_channel(c: float) -> float:
    if c <= 0.0:
        return 0.0
    if c >= 1.0:
        return 1.0
    return 12.92 * c if c <= 0.0031308 else 1.055 * (c ** (1.0 / 2.4)) - 0.055


def hex_to_rgb(value: str) -> RGB:
    text = value.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(ch * 2 for ch in text)
    if len(text) != 6:
        raise ValueError(f"not a hex color: {value!r}")
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def rgb_to_hex(rgb: RGB) -> str:
    r, g, b = (max(0, min(255, int(round(c)))) for c in rgb)
    return f"#{r:02x}{g:02x}{b:02x}"


# ---------------------------------------------------------------------------
# RGB <-> XYZ <-> Lab
# ---------------------------------------------------------------------------
def rgb_to_lab(rgb: RGB | tuple[float, float, float]) -> Lab:
    """sRGB (0-255) to CIELAB (D65). Accepts floats for intermediate values."""
    r, g, b = rgb
    rl = _srgb_to_linear_channel(max(0.0, min(1.0, r / 255.0)))
    gl = _srgb_to_linear_channel(max(0.0, min(1.0, g / 255.0)))
    bl = _srgb_to_linear_channel(max(0.0, min(1.0, b / 255.0)))

    x = _M_RGB_TO_XYZ[0][0] * rl + _M_RGB_TO_XYZ[0][1] * gl + _M_RGB_TO_XYZ[0][2] * bl
    y = _M_RGB_TO_XYZ[1][0] * rl + _M_RGB_TO_XYZ[1][1] * gl + _M_RGB_TO_XYZ[1][2] * bl
    z = _M_RGB_TO_XYZ[2][0] * rl + _M_RGB_TO_XYZ[2][1] * gl + _M_RGB_TO_XYZ[2][2] * bl
    return _xyz_to_lab(x, y, z)


def _f_lab(t: float) -> float:
    if t > _EPS:
        return t ** (1.0 / 3.0)
    return (_KAPPA * t + 16.0) / 116.0


def _xyz_to_lab(x: float, y: float, z: float) -> Lab:
    fx = _f_lab(x / _WHITE[0])
    fy = _f_lab(y / _WHITE[1])
    fz = _f_lab(z / _WHITE[2])
    return (116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz))


def _f_lab_inv(ft: float) -> float:
    ft3 = ft * ft * ft
    if ft3 > _EPS:
        return ft3
    return (116.0 * ft - 16.0) / _KAPPA


def lab_to_rgb(lab: Lab) -> RGB:
    """CIELAB to sRGB (0-255), clipped into the sRGB gamut."""
    L, a, b = lab
    fy = (L + 16.0) / 116.0
    fx = fy + a / 500.0
    fz = fy - b / 200.0
    x = _f_lab_inv(fx) * _WHITE[0]
    y = _f_lab_inv(fy) * _WHITE[1]
    z = _f_lab_inv(fz) * _WHITE[2]

    rl = _M_XYZ_TO_RGB[0][0] * x + _M_XYZ_TO_RGB[0][1] * y + _M_XYZ_TO_RGB[0][2] * z
    gl = _M_XYZ_TO_RGB[1][0] * x + _M_XYZ_TO_RGB[1][1] * y + _M_XYZ_TO_RGB[1][2] * z
    bl = _M_XYZ_TO_RGB[2][0] * x + _M_XYZ_TO_RGB[2][1] * y + _M_XYZ_TO_RGB[2][2] * z

    return (
        int(round(_linear_to_srgb_channel(rl) * 255)),
        int(round(_linear_to_srgb_channel(gl) * 255)),
        int(round(_linear_to_srgb_channel(bl) * 255)),
    )


def lab_lighten(lab: Lab, delta: float) -> Lab:
    return (max(0.0, min(100.0, lab[0] + delta)), lab[1], lab[2])


def lab_darken(lab: Lab, delta: float) -> Lab:
    return lab_lighten(lab, -delta)


def chroma(lab: Lab) -> float:
    return math.hypot(lab[1], lab[2])


# ---------------------------------------------------------------------------
# HSL (used by the UI for subtle lightness shifts)
# ---------------------------------------------------------------------------
def rgb_to_hsl(rgb: RGB) -> tuple[float, float, float]:
    r, g, b = (c / 255.0 for c in rgb)
    max_c = max(r, g, b)
    min_c = min(r, g, b)
    light = (max_c + min_c) / 2.0
    if max_c == min_c:
        return 0.0, 0.0, light
    d = max_c - min_c
    sat = d / (2.0 - max_c - min_c) if light > 0.5 else d / (max_c + min_c)
    if max_c == r:
        hue = ((g - b) / d + (6.0 if g < b else 0.0)) / 6.0
    elif max_c == g:
        hue = ((b - r) / d + 2.0) / 6.0
    else:
        hue = ((r - g) / d + 4.0) / 6.0
    return (hue * 360.0) % 360.0, sat, light


def hsl_to_rgb(hue: float, sat: float, light: float) -> RGB:
    hue = (hue % 360.0) / 360.0
    sat = max(0.0, min(1.0, sat))
    light = max(0.0, min(1.0, light))
    if sat == 0:
        v = int(round(light * 255))
        return (v, v, v)

    def hue2rgb(p: float, q: float, t: float) -> float:
        if t < 0:
            t += 1
        if t > 1:
            t -= 1
        if t < 1 / 6:
            return p + (q - p) * 6 * t
        if t < 1 / 2:
            return q
        if t < 2 / 3:
            return p + (q - p) * (2 / 3 - t) * 6
        return p

    q = light * (1 + sat) if light < 0.5 else light + sat - light * sat
    p = 2 * light - q
    return (
        int(round(hue2rgb(p, q, hue + 1 / 3) * 255)),
        int(round(hue2rgb(p, q, hue) * 255)),
        int(round(hue2rgb(p, q, hue - 1 / 3) * 255)),
    )


def adjust_lightness(rgb: RGB, delta: float) -> RGB:
    """Shift lightness by ``delta`` (0..1 scale), keeping hue and saturation."""
    h, s, l = rgb_to_hsl(rgb)
    return hsl_to_rgb(h, s, max(0.0, min(1.0, l + delta)))


def mix(a: RGB, b: RGB, t: float) -> RGB:
    """Linear interpolation between two colors; ``t=0`` -> a, ``t=1`` -> b."""
    t = max(0.0, min(1.0, t))
    return (
        int(round(a[0] + (b[0] - a[0]) * t)),
        int(round(a[1] + (b[1] - a[1]) * t)),
        int(round(a[2] + (b[2] - a[2]) * t)),
    )


# ---------------------------------------------------------------------------
# Contrast (WCAG 2.x)
# ---------------------------------------------------------------------------
def relative_luminance(rgb: RGB) -> float:
    rl = _srgb_to_linear_channel(rgb[0] / 255.0)
    gl = _srgb_to_linear_channel(rgb[1] / 255.0)
    bl = _srgb_to_linear_channel(rgb[2] / 255.0)
    return 0.2126 * rl + 0.7152 * gl + 0.0722 * bl


def contrast_ratio(fg: RGB, bg: RGB) -> float:
    l1 = relative_luminance(fg)
    l2 = relative_luminance(bg)
    lighter, darker = (l1, l2) if l1 >= l2 else (l2, l1)
    return (lighter + 0.05) / (darker + 0.05)


def ensure_contrast(fg: RGB, bg: RGB, target: float = 4.5) -> RGB:
    """Nudge ``fg`` lightness (away from ``bg``) until ``target`` is reached.

    Used when a derived background would leave primary text unreadable.
    """
    if contrast_ratio(fg, bg) >= target:
        return fg
    bg_lum = relative_luminance(bg)
    step = 1 if bg_lum < 0.5 else -1
    current = fg
    for _ in range(60):
        r, g, b = current
        if step > 0:
            current = (min(255, r + 4), min(255, g + 4), min(255, b + 4))
        else:
            current = (max(0, r - 4), max(0, g - 4), max(0, b - 4))
        if contrast_ratio(current, bg) >= target:
            return current
    return current


def ratio(fg_hex: str, bg_hex: str) -> float:
    return contrast_ratio(hex_to_rgb(fg_hex), hex_to_rgb(bg_hex))
