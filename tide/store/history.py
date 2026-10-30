"""Search history: the last 100 queries, most recent first.

Deduplicated - repeating a query moves it to the front. Stored as a versioned
JSON document written atomically.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tide.paths import Paths, ensure_dirs, get_paths
from tide.store.schema import (
    CURRENT_VERSION,
    atomic_write_json,
    load_json,
    migrate,
    utcnow,
)

MAX_HISTORY = 100


def _migrate_history_v1(doc: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: ``items`` becomes ``entries`` with timestamps dropped."""
    raw = doc.get("entries") or doc.get("items") or []
    entries: list[str] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                entries.append(item.strip())
            elif isinstance(item, dict) and isinstance(item.get("query"), str):
                entries.append(item["query"].strip())
    return {
        "schema_version": 2,
        "updated": str(doc.get("updated") or utcnow()),
        "entries": entries[:MAX_HISTORY],
    }


HISTORY_MIGRATIONS = {1: _migrate_history_v1}


class SearchHistory:
    def __init__(self, paths: Paths | None = None) -> None:
        self.paths = paths or get_paths()
        ensure_dirs(self.paths)
        self.entries: list[str] = self._load()

    @property
    def file(self) -> Path:
        return self.paths.history_file

    def _load(self) -> list[str]:
        data = load_json(self.file)
        if data is None:
            return []
        try:
            migrated, _ = migrate(data, HISTORY_MIGRATIONS, kind="history")
        except Exception:
            return []
        raw = migrated.get("entries", [])
        out: list[str] = []
        if isinstance(raw, list):
            for item in raw:
                if isinstance(item, str) and item.strip() and item not in out:
                    out.append(item.strip())
        return out[:MAX_HISTORY]

    def add(self, query: str) -> None:
        query = query.strip()
        if not query:
            return
        if query in self.entries:
            self.entries.remove(query)
        self.entries.insert(0, query)
        del self.entries[MAX_HISTORY:]
        self._save()

    def clear(self) -> None:
        self.entries.clear()
        self._save()

    def _save(self) -> None:
        atomic_write_json(
            self.file,
            {
                "schema_version": CURRENT_VERSION,
                "updated": utcnow(),
                "entries": self.entries,
            },
        )
