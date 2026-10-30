"""Shared yt-dlp plumbing: option building, a swappable factory, retries.

Tests replace :func:`open_ydl` with a fake extractor that returns canned
data, so no test ever touches the network.
"""

from __future__ import annotations

import random
import time
from typing import Any, Callable, TypeVar

from tide.log import get_logger

log = get_logger(__name__)

T = TypeVar("T")


class _YDLLogger:
    """Forward yt-dlp chatter into the rotating log file (never the terminal)."""

    def debug(self, msg: str) -> None:
        if not msg.startswith("[debug] "):
            log.debug("yt-dlp: %s", msg)
        else:
            log.debug("%s", msg)

    def info(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def warning(self, msg: str) -> None:
        log.warning("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.error("yt-dlp: %s", msg)


def base_opts(extra: dict[str, Any] | None = None) -> dict[str, Any]:
    """Consistent yt-dlp options: quiet, logged, network-timeout bounded."""
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "logger": _YDLLogger(),
        "socket_timeout": 15,
        "retries": 2,
        "skip_download": True,
    }
    if extra:
        opts.update(extra)
    return opts


def apply_network(opts: dict[str, Any], config: Any) -> dict[str, Any]:
    """Attach proxy/cookies from the config (cookie *path* only is logged)."""
    if config is not None:
        proxy = getattr(config, "proxy", None)
        if proxy:
            opts["proxy"] = proxy
        browser = getattr(config, "cookies_from_browser", None)
        if browser:
            opts["cookiesfrombrowser"] = (browser,)
            log.debug("cookies from browser: %s (contents never logged)", browser)
        else:
            cookie_path = None
            try:
                cookie_path = config.cookie_path
            except Exception:  # pragma: no cover - defensive
                cookie_path = None
            if cookie_path is not None:
                opts["cookiefile"] = str(cookie_path)
                log.debug("cookie file: %s (contents never logged)", cookie_path)
    return opts


def open_ydl(opts: dict[str, Any]) -> Any:
    """Factory seam for the fake extractor used in tests."""
    import yt_dlp

    return yt_dlp.YoutubeDL(opts)


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.4,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
    retry_on: tuple[type[BaseException], ...] = (Exception,),
) -> T:
    """Exponential backoff with jitter: ``attempts`` total tries.

    The sleeper and RNG are injectable so tests run instantly and
    deterministically.
    """
    generator = rng or random.Random()
    last: BaseException | None = None
    for attempt in range(1, max(1, attempts) + 1):
        try:
            return fn()
        except retry_on as exc:
            last = exc
            if attempt >= attempts:
                break
            delay = base_delay * (2 ** (attempt - 1)) + generator.uniform(0, base_delay)
            log.debug("retry %d/%d after %.2fs: %s", attempt, attempts, delay, exc)
            sleep(delay)
    assert last is not None  # noqa: S101 - loop always runs once
    raise last


def is_playlist_url(url: str) -> bool:
    lowered = url.lower()
    return "list=" in lowered or "/playlist?" in lowered or "playlist?" in lowered
