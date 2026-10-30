"""Rotating file logging.

Logs never reach the terminal: the UI keeps its status line and the CLI keeps
stdout clean. Only a rotating file handler in the data directory is used.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

from tide.paths import Paths, ensure_dirs, get_paths

_CONFIGURED = False
ROOT_NAME = "tide"

_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
_DATEFMT = "%H:%M:%S"

_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


def resolve_level(name: str) -> int:
    return _LEVELS.get(str(name).strip().lower(), logging.INFO)


def setup_logging(level: str = "info", paths: Paths | None = None) -> Path:
    """Configure the root ``tide`` logger. Safe to call more than once."""
    global _CONFIGURED
    paths = paths or get_paths()
    ensure_dirs(paths)
    log_file = paths.log_file

    root = logging.getLogger(ROOT_NAME)
    root.setLevel(resolve_level(level))

    if _CONFIGURED:
        return log_file

    handler = logging.handlers.RotatingFileHandler(
        log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8", delay=True
    )
    handler.setFormatter(logging.Formatter(_FORMAT, datefmt=_DATEFMT))
    root.addHandler(handler)
    # Never propagate to the root logger: that is what prints to the terminal.
    root.propagate = False
    _CONFIGURED = True
    return log_file


def get_logger(name: str = "") -> logging.Logger:
    """Return a namespaced logger under the ``tide`` root."""
    if name.startswith(ROOT_NAME):
        return logging.getLogger(name)
    if not name:
        return logging.getLogger(ROOT_NAME)
    return logging.getLogger(f"{ROOT_NAME}.{name}")


def log_file_path() -> Path:
    return get_paths().log_file
