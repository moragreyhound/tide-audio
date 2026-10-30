"""Versioned JSON documents with atomic writes and migrations.

Every persisted JSON file carries ``schema_version``. Writes are atomic
(temp file in the same directory -> flush -> fsync -> rename) and keep a
``.bak`` of the previous content before any destructive replace.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from tide.log import get_logger

log = get_logger(__name__)

CURRENT_VERSION = 2

Migrations = dict[int, Callable[[dict[str, Any]], dict[str, Any]]]


class SchemaError(ValueError):
    """Raised when a document cannot be brought up to the current version."""


def utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------
def atomic_write_json(path: Path, data: dict[str, Any], *, backup: bool = True) -> None:
    """Write JSON atomically, keeping ``path.bak`` of the previous file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=False)
    return atomic_write_text(path, payload, backup=backup)


def atomic_write_text(path: Path, text: str, *, backup: bool = True) -> None:
    """Write ``text`` atomically with fsync, keeping a ``.bak`` copy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if backup and path.exists():
        bak = path.with_suffix(path.suffix + ".bak")
        try:
            bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - best effort
            log.debug("could not write backup for %s: %s", path, exc)

    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
        raise


def load_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON object; returns ``None`` when missing or unreadable."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        log.warning("could not read %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("%s is not a JSON object", path)
        return None
    return data


# ---------------------------------------------------------------------------
# Versioning
# ---------------------------------------------------------------------------
def detect_version(data: dict[str, Any]) -> int:
    """Version of a document; legacy files used ``version`` or none."""
    for key in ("schema_version", "version"):
        value = data.get(key)
        if isinstance(value, int) and value > 0:
            return value
    return 1


def migrate(
    data: dict[str, Any],
    migrations: Migrations,
    *,
    kind: str = "document",
    target: int = CURRENT_VERSION,
) -> tuple[dict[str, Any], list[str]]:
    """Bring ``data`` to ``target`` version.

    Returns ``(migrated_document, notes)``. Raises :class:`SchemaError` when a
    migration step is missing or a document is from the future.
    """
    version = detect_version(data)
    notes: list[str] = []
    if version > target:
        raise SchemaError(
            f"{kind} schema_version {version} is newer than supported {target}; "
            "upgrade Tide or remove the file"
        )
    working = dict(data)
    while version < target:
        step = migrations.get(version)
        if step is None:
            raise SchemaError(f"no migration from {kind} schema_version {version}")
        working = step(working)
        next_version = version + 1
        notes.append(f"{kind} migrated {version} -> {next_version}")
        version = next_version
    working["schema_version"] = target
    return working, notes


# ---------------------------------------------------------------------------
# Shared migrations
# ---------------------------------------------------------------------------
def _upgrade_timestamps(doc: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: adopt schema_version, add timestamps where missing."""
    out = dict(doc)
    out["schema_version"] = 2
    now = utcnow()
    out.setdefault("created", now)
    out.setdefault("updated", now)
    return out


BASE_MIGRATIONS: Migrations = {1: _upgrade_timestamps}
