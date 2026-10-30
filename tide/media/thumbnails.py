"""Artwork: pick the best thumbnail URL, fetch with cache, square-crop covers.

Fetches are bounded (timeout, size cap, two retries) and cached by URL hash
under the cache directory. Cookie and proxy settings follow the config; the
image bytes themselves are never logged.
"""

from __future__ import annotations

import hashlib
import io
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from tide.log import get_logger
from tide.paths import Paths, ensure_dirs, get_paths

log = get_logger(__name__)

MAX_BYTES = 5 * 1024 * 1024
TIMEOUT = 10.0
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) Tide/0.1"
_PREFERRED_MAX_WIDTH = 1280


def best_thumbnail_url(thumbnails: list[Any]) -> str | None:
    """Highest useful resolution: largest image up to a sane width first.

    Falls back to the largest overall when everything is bigger than
    ``_PREFERRED_MAX_WIDTH`` (never returns a 4K monster when an 800px
    variant exists).
    """
    candidates: list[tuple[int, int, str]] = []
    for entry in thumbnails:
        if not isinstance(entry, dict):
            continue
        url = entry.get("url")
        if not isinstance(url, str) or not url:
            continue
        width = entry.get("width") or 0
        height = entry.get("height") or 0
        preference = entry.get("preference")
        width = int(width) if isinstance(width, (int, float)) else 0
        height = int(height) if isinstance(height, (int, float)) else 0
        preference = int(preference) if isinstance(preference, (int, float)) else 0
        area = width * height if width and height else 1
        candidates.append((preference, area, url))
    if not candidates:
        return None
    preferred = [c for c in candidates if c[1] <= _PREFERRED_MAX_WIDTH * _PREFERRED_MAX_WIDTH]
    pool = preferred or candidates
    pool.sort()
    return pool[-1][2]


def _cache_path(url: str, paths: Paths) -> Path:
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:40]
    return paths.thumbnails_cache_dir / f"{digest}.bin"


def _opener(config: Any) -> Any:
    handlers: list[Any] = []
    proxy = getattr(config, "proxy", None)
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    return urllib.request.build_opener(*handlers)


def fetch_thumbnail(
    url: str,
    *,
    config: Any = None,
    paths: Paths | None = None,
    force: bool = False,
) -> bytes | None:
    """Download artwork bytes (cached). Returns ``None`` on any failure."""
    paths = paths or get_paths()
    ensure_dirs(paths)
    cache = _cache_path(url, paths)
    if not force:
        try:
            cached = cache.read_bytes()
            if cached:
                return cached
        except OSError:
            pass

    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(2):
        try:
            with _opener(config).open(request, timeout=TIMEOUT) as response:
                data = response.read(MAX_BYTES + 1)
            if len(data) > MAX_BYTES:
                log.warning("thumbnail larger than %d bytes, skipping", MAX_BYTES)
                return None
            if not data:
                return None
            try:
                cache.write_bytes(data)
            except OSError:  # pragma: no cover - read-only cache
                pass
            return data
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            if attempt == 1:
                log.debug("thumbnail fetch failed: %s", exc)
                return None
    return None


def square_crop(data: bytes) -> bytes:
    """Center-crop artwork to a square JPEG (for cover-art embedding)."""
    from PIL import Image

    image = Image.open(io.BytesIO(data)).convert("RGB")
    edge = min(image.size)
    left = (image.width - edge) // 2
    top = (image.height - edge) // 2
    cropped = image.crop((left, top, left + edge, top + edge))
    if cropped.width > 1024:
        cropped = cropped.resize((1024, 1024), Image.LANCZOS)
    out = io.BytesIO()
    cropped.save(out, format="JPEG", quality=90)
    return out.getvalue()


def write_cover(data: bytes, target: Path) -> Path:
    """Write a square cover next to ``target`` (caller removes it)."""
    target.write_bytes(square_crop(data))
    return target
