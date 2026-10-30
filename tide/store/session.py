"""Playback session persistence.

Saves the current track, position, queue, volume, shuffle/repeat/speed and
panel state so a restart resumes where the app stopped. Disabled entirely by
``restore_session = false``; corrupted files are ignored rather than fatal.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tide.log import get_logger
from tide.paths import Paths, ensure_dirs, get_paths
from tide.store.schema import (
    CURRENT_VERSION,
    atomic_write_json,
    load_json,
    migrate,
    utcnow,
)
from tide.store.playlists import Track

log = get_logger(__name__)

REPEAT_MODES = ("off", "queue", "one")


def _migrate_session_v1(doc: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: flat keys move under ``state``; queue entries gain schema."""
    state = dict(doc)
    state.pop("schema_version", None)
    state.pop("version", None)
    old_queue = state.pop("queue", None)
    if isinstance(old_queue, list):
        state["queue"] = [t for t in (Track.from_dict(x).to_dict() if Track.from_dict(x) else None for x in old_queue) if t]
    return {"schema_version": 2, "updated": str(doc.get("updated") or utcnow()), "state": state}


SESSION_MIGRATIONS = {1: _migrate_session_v1}


@dataclass
class SessionState:
    track: dict[str, Any] | None = None
    position: float = 0.0
    queue: list[dict[str, Any]] = field(default_factory=list)
    index: int = 0
    volume: float = 0.8
    muted: bool = False
    shuffle: bool = False
    repeat: str = "off"
    speed: float = 1.0
    eq_preset: str = "flat"
    eq_gains: list[float] = field(default_factory=lambda: [0.0] * 10)
    visualizer: str = "wash"
    rail_visible: bool = True
    view: str = "search"

    def to_dict(self) -> dict[str, Any]:
        return {
            "track": self.track,
            "position": round(float(self.position), 2),
            "queue": self.queue,
            "index": int(self.index),
            "volume": round(float(self.volume), 3),
            "muted": bool(self.muted),
            "shuffle": bool(self.shuffle),
            "repeat": self.repeat if self.repeat in REPEAT_MODES else "off",
            "speed": float(self.speed),
            "eq_preset": self.eq_preset,
            "eq_gains": [round(float(g), 2) for g in self.eq_gains],
            "visualizer": self.visualizer,
            "rail_visible": bool(self.rail_visible),
            "view": self.view,
        }

    @property
    def current_track(self) -> Track | None:
        if not self.track:
            return None
        return Track.from_dict(self.track)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SessionState":
        def _num(value: Any, default: float, lo: float, hi: float) -> float:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return default
            return max(lo, min(hi, float(value)))

        queue_raw = data.get("queue")
        queue: list[dict[str, Any]] = []
        if isinstance(queue_raw, list):
            for item in queue_raw:
                track = Track.from_dict(item)
                if track is not None:
                    queue.append(track.to_dict())
        track = data.get("track")
        track_dict = None
        if isinstance(track, dict):
            parsed = Track.from_dict(track)
            track_dict = parsed.to_dict() if parsed else None
        gains = data.get("eq_gains")
        if not isinstance(gains, list) or len(gains) != 10:
            gains = [0.0] * 10
        return cls(
            track=track_dict,
            position=_num(data.get("position"), 0.0, 0.0, 24 * 3600),
            queue=queue,
            index=int(data.get("index")) if isinstance(data.get("index"), int) else 0,
            volume=_num(data.get("volume"), 0.8, 0.0, 1.0),
            muted=bool(data.get("muted", False)),
            shuffle=bool(data.get("shuffle", False)),
            repeat=str(data.get("repeat", "off")) if data.get("repeat") in REPEAT_MODES else "off",
            speed=_num(data.get("speed"), 1.0, 0.5, 2.0),
            eq_preset=str(data.get("eq_preset", "flat")),
            eq_gains=[_num(g, 0.0, -12.0, 12.0) for g in gains],
            visualizer=str(data.get("visualizer", "wash")),
            rail_visible=bool(data.get("rail_visible", True)),
            view=str(data.get("view", "search")),
        )


class SessionStore:
    def __init__(self, paths: Paths | None = None, enabled: bool = True) -> None:
        self.paths = paths or get_paths()
        ensure_dirs(self.paths)
        self.enabled = enabled

    @property
    def file(self) -> Path:
        return self.paths.session_file

    def load(self) -> SessionState | None:
        if not self.enabled:
            return None
        data = load_json(self.file)
        if data is None:
            return None
        try:
            migrated, notes = migrate(data, SESSION_MIGRATIONS, kind="session")
        except Exception as exc:
            log.warning("session state unusable: %s", exc)
            return None
        for note in notes:
            log.info(note)
        state = migrated.get("state")
        if not isinstance(state, dict):
            return None
        try:
            return SessionState.from_dict(state)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            log.warning("session state ignored: %s", exc)
            return None

    def save(self, state: SessionState) -> bool:
        if not self.enabled:
            return False
        try:
            atomic_write_json(
                self.file,
                {"schema_version": CURRENT_VERSION, "updated": utcnow(), "state": state.to_dict()},
            )
            return True
        except OSError as exc:  # pragma: no cover - disk full etc.
            log.error("could not save session: %s", exc)
            return False

    def clear(self) -> None:
        try:
            self.file.unlink(missing_ok=True)
        except OSError:  # pragma: no cover
            pass
