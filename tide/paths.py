"""Platform-aware filesystem locations for Tide.

All paths used by the application are resolved here so no other module has to
know about platformdirs or environment overrides.

Environment overrides
---------------------
``TIDE_CONFIG_DIR``  override the config directory
``TIDE_DATA_DIR``    override the data directory
``TIDE_CACHE_DIR``   override the cache directory
``TIDE_CONFIG``      full path to ``config.toml``
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "tide"


@dataclass(frozen=True)
class Paths:
    """Resolved locations for configuration, data, cache and logs."""

    config_dir: Path
    data_dir: Path
    cache_dir: Path

    @property
    def config_file(self) -> Path:
        override = os.environ.get("TIDE_CONFIG")
        if override:
            return Path(override).expanduser()
        return self.config_dir / "config.toml"

    @property
    def keys_file(self) -> Path:
        return self.config_dir / "keys.toml"

    @property
    def playlists_dir(self) -> Path:
        return self.data_dir / "playlists"

    @property
    def history_file(self) -> Path:
        return self.data_dir / "history.json"

    @property
    def session_file(self) -> Path:
        return self.data_dir / "session.json"

    @property
    def palette_cache_dir(self) -> Path:
        return self.cache_dir / "palettes"

    @property
    def thumbnails_cache_dir(self) -> Path:
        return self.cache_dir / "thumbnails"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def log_file(self) -> Path:
        return self.log_dir / "tide.log"

    @property
    def cookies_candidates(self) -> tuple[Path, ...]:
        """Locations probed for a Netscape-format ``cookies.txt``.

        The working directory comes first so ``tide`` run next to an exported
        cookie file picks it up automatically.
        """
        return (
            Path.cwd() / "cookies.txt",
            self.config_dir / "cookies.txt",
            self.data_dir / "cookies.txt",
        )


def get_paths() -> Paths:
    """Resolve :class:`Paths` for the current process (cached by the caller)."""
    from platformdirs import user_cache_dir, user_config_dir, user_data_dir

    config_dir = Path(
        os.environ.get("TIDE_CONFIG_DIR")
        or user_config_dir(APP_NAME, appauthor=False)
    ).expanduser()
    data_dir = Path(
        os.environ.get("TIDE_DATA_DIR")
        or user_data_dir(APP_NAME, appauthor=False)
    ).expanduser()
    cache_dir = Path(
        os.environ.get("TIDE_CACHE_DIR")
        or user_cache_dir(APP_NAME, appauthor=False)
    ).expanduser()
    return Paths(config_dir=config_dir, data_dir=data_dir, cache_dir=cache_dir)


def ensure_dirs(paths: Paths) -> None:
    """Create every directory Tide writes to (idempotent, failure-tolerant)."""
    for directory in (
        paths.config_dir,
        paths.data_dir,
        paths.cache_dir,
        paths.playlists_dir,
        paths.palette_cache_dir,
        paths.thumbnails_cache_dir,
        paths.log_dir,
    ):
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:  # pragma: no cover - read-only media
            pass


def find_cookiefile(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Locate a Netscape cookie file for yt-dlp.

    Order: explicit config value, then ``cookies.txt`` in the working
    directory, the config directory, and the data directory. Returns ``None``
    when nothing exists. The file contents are never logged.
    """
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    for candidate in get_paths().cookies_candidates:
        try:
            if candidate.is_file():
                return candidate
        except OSError:  # pragma: no cover - exotic filesystems
            continue
    return None


def is_windows() -> bool:
    return sys.platform.startswith("win")
