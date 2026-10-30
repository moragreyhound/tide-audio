"""Palette extraction: artwork -> k-means clusters in CIELAB.

Pipeline (see docs/DESIGN.md):

1. decode artwork, downscale to 64x64 (Lanczos);
2. convert every pixel to Lab (vectorized NumPy);
3. drop near-neutral and extreme-lightness pixels so white/black never win;
4. k-means with k=6, 10 iterations, a fixed seed - deterministic output;
5. pick the accent as the cluster with the highest chroma times a lightness
   penalty, the dominant cluster by size, and derive a deep background by
   moving the dominant color into a dark lightness band and verifying
   WCAG-style contrast against the primary ink.

Results are cached under the cache directory keyed by the SHA-256 of the
source bytes.
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from tide.paths import Paths, ensure_dirs, get_paths
from tide.visual.color import (
    RGB,
    chroma,
    contrast_ratio,
    ensure_contrast,
    lab_to_rgb,
    rgb_to_hex,
    rgb_to_lab,
)

# Fallback accent when artwork is missing: a muted sage (light) and a warm
# ink tone (dark). Never hardcoded into the UI as the primary accent.
SAGE = RGB(0x9C, 0xAF, 0x88)
WARM_INK = RGB(0xD8, 0xD2, 0xC4)

PRIMARY_INK = RGB(232, 230, 224)  # the text color backgrounds are checked against
DARK_BAND = (9.0, 15.0)  # Lab L targets for dark backgrounds
LIGHT_BAND = (88.0, 94.0)

_K = 6
_ITERATIONS = 10
_SEED = 20260101


@dataclass(frozen=True)
class Palette:
    """Five to seven colors derived from artwork, plus semantic picks."""

    colors: tuple[RGB, ...]
    accent: RGB
    dominant: RGB
    background: RGB
    background_light: RGB
    source_hash: str = ""

    @property
    def deep(self) -> RGB:
        return self.background

    @classmethod
    def fallback(cls) -> "Palette":
        """Palette used when there is no artwork (muted sage accent)."""
        dark = (16, 17, 16)
        return cls(
            colors=(SAGE, (120, 132, 108), (74, 82, 68), (40, 44, 38), (28, 30, 27)),
            accent=SAGE,
            dominant=SAGE,
            background=dark,
            background_light=(242, 241, 237),
            source_hash="fallback",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "colors": [rgb_to_hex(c) for c in self.colors],
            "accent": rgb_to_hex(self.accent),
            "dominant": rgb_to_hex(self.dominant),
            "background": rgb_to_hex(self.background),
            "background_light": rgb_to_hex(self.background_light),
            "source_hash": self.source_hash,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Palette":
        from tide.visual.color import hex_to_rgb

        colors = tuple(hex_to_rgb(c) for c in data.get("colors", []))
        accent = hex_to_rgb(str(data.get("accent", "#9caf88")))
        dominant = hex_to_rgb(str(data.get("dominant", "#9caf88")))
        background = hex_to_rgb(str(data.get("background", "#101110")))
        background_light = hex_to_rgb(str(data.get("background_light", "#f2f1ed")))
        return cls(
            colors=colors or (accent,),
            accent=accent,
            dominant=dominant,
            background=background,
            background_light=background_light,
            source_hash=str(data.get("source_hash", "")),
        )


# ---------------------------------------------------------------------------
# Color space helpers (vectorized)
# ---------------------------------------------------------------------------
def rgb_array_to_lab(pixels: np.ndarray) -> np.ndarray:
    """Vectorized RGB -> Lab for an ``(N, 3)`` float/uint8 array."""
    arr = np.asarray(pixels, dtype=np.float64)
    if arr.size and arr.max() > 1.5:
        arr = arr / 255.0
    linear = np.where(arr <= 0.04045, arr / 12.92, ((arr + 0.055) / 1.055) ** 2.4)

    m = np.array(
        [
            [0.4124564, 0.3575761, 0.1804375],
            [0.2126729, 0.7151522, 0.0721750],
            [0.0193339, 0.1191920, 0.9503041],
        ]
    )
    white = np.array([0.95047, 1.00000, 1.08883])
    xyz = linear @ m.T
    ratio = xyz / white
    eps = 216.0 / 24389.0
    kappa = 24389.0 / 27.0
    f = np.where(ratio > eps, np.cbrt(np.maximum(ratio, 0.0)), (kappa * ratio + 16.0) / 116.0)
    fx, fy, fz = f[:, 0], f[:, 1], f[:, 2]
    return np.stack(
        [116.0 * fy - 16.0, 500.0 * (fx - fy), 200.0 * (fy - fz)], axis=1
    )


# ---------------------------------------------------------------------------
# k-means (deterministic)
# ---------------------------------------------------------------------------
def kmeans_lab(
    data: np.ndarray,
    k: int = _K,
    iterations: int = _ITERATIONS,
    seed: int = _SEED,
) -> tuple[np.ndarray, np.ndarray]:
    """k-means over Lab points. Returns ``(centers, counts)``.

    Uses k-means++ seeding driven by a seeded Generator, so identical input
    always yields identical centers.
    """
    n = data.shape[0]
    k = max(1, min(k, n))
    rng = np.random.default_rng(seed)

    centers = np.empty((k, data.shape[1]), dtype=np.float64)
    # k-means++ seeding (deterministic given the seed)
    first = int(rng.integers(0, n))
    centers[0] = data[first]
    closest = ((data - centers[0]) ** 2).sum(axis=1)
    for i in range(1, k):
        total = float(closest.sum())
        if total <= 1e-12:
            centers[i] = data[int(rng.integers(0, n))]
        else:
            probs = closest / total
            idx = int(rng.choice(n, p=probs))
            centers[i] = data[idx]
        closest = np.minimum(closest, ((data - centers[i]) ** 2).sum(axis=1))

    labels = np.zeros(n, dtype=np.int64)
    for _ in range(max(1, iterations)):
        # assignment
        d2 = ((data[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        labels = d2.argmin(axis=1)
        # update with empty-cluster repair
        new_centers = centers.copy()
        for j in range(k):
            mask = labels == j
            if np.any(mask):
                new_centers[j] = data[mask].mean(axis=0)
            else:
                worst = int(d2.max(axis=1).argmax())
                new_centers[j] = data[worst]
        shift = float(np.abs(new_centers - centers).max())
        centers = new_centers
        if shift < 1e-4:
            break

    counts = np.bincount(labels, minlength=k).astype(np.int64)
    return centers, counts


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------
def load_artwork(source: bytes | Path | Any) -> Any:
    """Decode artwork from bytes or a path into an RGB Pillow image."""
    from PIL import Image

    if isinstance(source, (bytes, bytearray)):
        img = Image.open(io.BytesIO(bytes(source)))
    elif isinstance(source, (str, Path)):
        img = Image.open(str(source))
    else:
        img = source  # already a Pillow image
    return img.convert("RGB")


def _downscale(img: Any) -> Any:
    from PIL import Image

    if img.size == (64, 64):
        return img
    return img.resize((64, 64), Image.LANCZOS)


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------
def _lightness_penalty(L: float) -> float:
    """1.0 inside a comfortable range, fading toward pure white/black."""
    return 1.0 - min(1.0, abs(L - 55.0) / 45.0)


def _pick_accent(centers: np.ndarray, counts: np.ndarray) -> RGB:
    best_score = -1.0
    best = centers[int(np.argmax(counts))]
    for center in centers:
        L, a, b = float(center[0]), float(center[1]), float(center[2])
        chroma_val = (a * a + b * b) ** 0.5
        score = chroma_val * _lightness_penalty(L)
        if score > best_score:
            best_score = score
            best = center
    if best_score <= 1.0:  # effectively neutral (grayscale artwork)
        return WARM_INK
    return lab_to_rgb((float(best[0]), float(best[1]), float(best[2])))


def _derive_background(dominant: RGB, *, light: bool) -> RGB:
    lab = rgb_to_lab(dominant)
    band = LIGHT_BAND if light else DARK_BAND
    # Keep a trace of the artwork's hue: pull chroma toward neutral.
    target_l = (band[0] + band[1]) / 2.0
    scale = 0.45 if light else 0.35
    bg = lab_to_rgb((target_l, lab[1] * scale, lab[2] * scale))

    # Primary text must remain readable on this background.
    probe = RGB(24, 24, 26) if light else PRIMARY_INK
    if contrast_ratio(probe, bg) < 4.5:
        if light:
            bg = ensure_contrast(bg, probe, 4.5)
        else:
            cur = rgb_to_lab(bg)
            for _ in range(48):
                if contrast_ratio(PRIMARY_INK, bg) >= 4.5:
                    break
                cur = (max(0.0, cur[0] - 2.0), cur[1] * 0.92, cur[2] * 0.92)
                bg = lab_to_rgb(cur)
    return bg


def extract_palette(source: bytes | Path | Any, *, seed: int = _SEED) -> Palette:
    """Extract a :class:`Palette` from artwork bytes, a path, or an image."""
    img = _downscale(load_artwork(source))
    pixels = np.asarray(img, dtype=np.float32).reshape(-1, 3)
    lab = rgb_array_to_lab(pixels)

    chroma_vals = np.hypot(lab[:, 1], lab[:, 2])
    L = lab[:, 0]
    keep = (chroma_vals > 8.0) & (L > 12.0) & (L < 96.0)
    filtered = lab[keep]
    if filtered.shape[0] < _K * 4:
        filtered = lab  # grayscale or low-variance artwork: cluster everything

    centers, counts = kmeans_lab(filtered, k=_K, seed=seed)

    order = np.argsort(-counts)
    ordered = centers[order]
    colors = tuple(
        lab_to_rgb((float(c[0]), float(c[1]), float(c[2]))) for c in ordered
    )

    dominant = colors[0]
    accent = _pick_accent(centers, counts)
    background = _derive_background(dominant, light=False)
    background_light = _derive_background(dominant, light=True)

    raw = source if isinstance(source, (bytes, bytearray)) else None
    source_hash = (
        hashlib.sha256(bytes(raw)).hexdigest()
        if raw is not None
        else hashlib.sha256(img.tobytes()).hexdigest()
    )
    return Palette(
        colors=colors,
        accent=accent,
        dominant=dominant,
        background=background,
        background_light=background_light,
        source_hash=source_hash,
    )


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------
def _cache_path(source_hash: str, paths: Paths) -> Path:
    return paths.palette_cache_dir / f"{source_hash}.json"


def load_cached_palette(source: bytes, paths: Paths | None = None) -> Palette | None:
    paths = paths or get_paths()
    digest = hashlib.sha256(bytes(source)).hexdigest()
    path = _cache_path(digest, paths)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    try:
        return Palette.from_dict(data)
    except (ValueError, KeyError):
        return None


def extract_palette_cached(
    source: bytes, paths: Paths | None = None, *, seed: int = _SEED
) -> Palette:
    """Extract with a SHA-256 keyed disk cache (reads never recompute k-means)."""
    paths = paths or get_paths()
    cached = load_cached_palette(source, paths)
    if cached is not None:
        return cached
    palette = extract_palette(source, seed=seed)
    ensure_dirs(paths)
    try:
        _cache_path(palette.source_hash, paths).write_text(
            json.dumps(palette.to_dict()), encoding="utf-8"
        )
    except OSError:  # pragma: no cover - read-only cache dir
        pass
    return palette


def hex_palette(palette: Palette) -> dict[str, str]:
    return palette.to_dict()
