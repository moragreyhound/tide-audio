"""Playlist storage: one JSON file per playlist under the data directory.

Never stores expiring direct media URLs - only stable ``source`` identifiers
(video URL, watch page, or local path). The special ``queue`` playlist is
always present and cannot be deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from tide.log import get_logger
from tide.paths import Paths, ensure_dirs, get_paths
from tide.store.schema import (
    CURRENT_VERSION,
    atomic_write_json,
    detect_version,
    load_json,
    migrate,
    utcnow,
)
from tide.textutil import slugify

log = get_logger(__name__)

QUEUE_ID = "queue"
QUEUE_NAME = "Queue"


@dataclass
class Track:
    """One entry. ``source`` is a URL or local path - never a signed URL."""

    source: str
    title: str = ""
    channel: str = ""
    duration: int | None = None
    thumbnail: str | None = None
    added: str = field(default_factory=utcnow)

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "channel": self.channel,
            "duration": self.duration,
            "thumbnail": self.thumbnail,
            "added": self.added,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Track | None":
        if not isinstance(data, dict):
            return None
        source = data.get("source") or data.get("url") or data.get("id")
        if not isinstance(source, str) or not source.strip():
            return None
        duration = data.get("duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            duration = None
        return cls(
            source=source.strip(),
            title=str(data.get("title") or ""),
            channel=str(data.get("channel") or data.get("uploader") or ""),
            duration=int(duration) if duration is not None else None,
            thumbnail=data.get("thumbnail") if isinstance(data.get("thumbnail"), str) else None,
            added=str(data.get("added") or utcnow()),
        )


def _migrate_playlist_v1(doc: dict[str, Any]) -> dict[str, Any]:
    """v1 -> v2: ``version`` -> ``schema_version``, ``entries`` -> ``tracks``."""
    out: dict[str, Any] = {
        "schema_version": 2,
        "id": str(doc.get("id") or slugify(str(doc.get("name") or "playlist"))),
        "name": str(doc.get("name") or "Playlist"),
        "created": str(doc.get("created") or utcnow()),
        "updated": str(doc.get("updated") or utcnow()),
    }
    raw_tracks = doc.get("tracks") or doc.get("entries") or []
    tracks: list[dict[str, Any]] = []
    if isinstance(raw_tracks, list):
        for item in raw_tracks:
            if not isinstance(item, dict):
                continue
            track = Track.from_dict(item)
            if track:
                tracks.append(track.to_dict())
    out["tracks"] = tracks
    return out


PLAYLIST_MIGRATIONS = {1: _migrate_playlist_v1}


@dataclass
class Playlist:
    id: str
    name: str
    created: str = field(default_factory=utcnow)
    updated: str = field(default_factory=utcnow)
    tracks: list[Track] = field(default_factory=list)

    @property
    def is_queue(self) -> bool:
        return self.id == QUEUE_ID

    @property
    def total_duration(self) -> int:
        return sum(t.duration or 0 for t in self.tracks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": CURRENT_VERSION,
            "id": self.id,
            "name": self.name,
            "created": self.created,
            "updated": self.updated,
            "tracks": [t.to_dict() for t in self.tracks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Playlist":
        migrated, notes = migrate(data, PLAYLIST_MIGRATIONS, kind="playlist")
        for note in notes:
            log.info(note)
        tracks: list[Track] = []
        raw = migrated.get("tracks", [])
        if isinstance(raw, list):
            for item in raw:
                track = Track.from_dict(item)
                if track is not None:
                    tracks.append(track)
        return cls(
            id=str(migrated.get("id") or slugify(str(migrated.get("name") or "playlist"))),
            name=str(migrated.get("name") or "Playlist"),
            created=str(migrated.get("created") or utcnow()),
            updated=str(migrated.get("updated") or utcnow()),
            tracks=tracks,
        )


class PlaylistStore:
    """Filesystem-backed collection of playlists (including the queue)."""

    def __init__(self, paths: Paths | None = None) -> None:
        self.paths = paths or get_paths()
        ensure_dirs(self.paths)
        self._cache: dict[str, Playlist] = {}
        self._load_all()

    # -- loading ---------------------------------------------------------
    @property
    def directory(self) -> Path:
        return self.paths.playlists_dir

    def _file(self, playlist_id: str) -> Path:
        return self.directory / f"{slugify(playlist_id)}.json"

    def _load_all(self) -> None:
        found: dict[str, Playlist] = {}
        try:
            files = sorted(self.directory.glob("*.json"))
        except OSError:  # pragma: no cover
            files = []
        for path in files:
            data = load_json(path)
            if data is None:
                continue
            try:
                playlist = Playlist.from_dict(data)
            except Exception as exc:
                log.warning("skipping unreadable playlist %s: %s", path.name, exc)
                continue
            found[playlist.id] = playlist
        if QUEUE_ID not in found:
            found[QUEUE_ID] = Playlist(id=QUEUE_ID, name=QUEUE_NAME)
            self._write(found[QUEUE_ID])
        self._cache = found

    # -- queries ---------------------------------------------------------
    def all(self) -> list[Playlist]:
        items = list(self._cache.values())
        items.sort(key=lambda p: (p.is_queue is False, p.created))
        return items

    def get(self, playlist_id: str) -> Playlist | None:
        return self._cache.get(playlist_id)

    def by_name(self, name: str) -> Playlist | None:
        low = name.lower()
        for playlist in self._cache.values():
            if playlist.name.lower() == low:
                return playlist
        return None

    @property
    def queue(self) -> Playlist:
        return self._cache[QUEUE_ID]

    # -- mutation --------------------------------------------------------
    def _unique_id(self, name: str) -> str:
        base = slugify(name)
        candidate = base
        suffix = 2
        while candidate in self._cache:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def create(self, name: str) -> Playlist:
        name = name.strip() or "Playlist"
        existing = self.by_name(name)
        if existing is not None:
            return existing
        playlist = Playlist(id=self._unique_id(name), name=name)
        self._cache[playlist.id] = playlist
        self._write(playlist)
        return playlist

    def rename(self, playlist_id: str, name: str) -> Playlist | None:
        playlist = self._cache.get(playlist_id)
        if playlist is None:
            return None
        name = name.strip() or playlist.name
        if playlist.is_queue:
            return playlist  # the queue keeps its name
        old_file = self._file(playlist.id)
        playlist.name = name
        new_id = self._unique_id(name)
        if new_id != playlist.id:
            playlist.id = new_id
            try:
                old_file.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
        playlist.updated = utcnow()
        self._cache[playlist.id] = playlist
        self._write(playlist)
        return playlist

    def delete(self, playlist_id: str) -> bool:
        if playlist_id == QUEUE_ID:
            return False
        playlist = self._cache.pop(playlist_id, None)
        if playlist is None:
            return False
        target = self._file(playlist_id)
        try:
            if target.exists():
                target.unlink()
            bak = target.with_suffix(target.suffix + ".bak")
            bak.unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover
            log.warning("could not delete %s: %s", target, exc)
            self._cache[playlist_id] = playlist
            return False
        return True

    def add_track(self, playlist_id: str, track: Track, index: int | None = None) -> bool:
        playlist = self._cache.get(playlist_id)
        if playlist is None:
            return False
        track.added = utcnow()
        if index is None:
            playlist.tracks.append(track)
        else:
            playlist.tracks.insert(max(0, min(index, len(playlist.tracks))), track)
        playlist.updated = utcnow()
        self._write(playlist)
        return True

    def remove_track(self, playlist_id: str, index: int) -> bool:
        playlist = self._cache.get(playlist_id)
        if playlist is None or not (0 <= index < len(playlist.tracks)):
            return False
        playlist.tracks.pop(index)
        playlist.updated = utcnow()
        self._write(playlist)
        return True

    def move_track(self, playlist_id: str, index: int, delta: int) -> bool:
        playlist = self._cache.get(playlist_id)
        if playlist is None:
            return False
        target = index + delta
        if not (0 <= index < len(playlist.tracks)) or not (0 <= target < len(playlist.tracks)):
            return False
        tracks = playlist.tracks
        tracks[index], tracks[target] = tracks[target], tracks[index]
        playlist.updated = utcnow()
        self._write(playlist)
        return True

    def set_tracks(self, playlist_id: str, tracks: Iterable[Track]) -> bool:
        playlist = self._cache.get(playlist_id)
        if playlist is None:
            return False
        playlist.tracks = list(tracks)
        playlist.updated = utcnow()
        self._write(playlist)
        return True

    # -- persistence -----------------------------------------------------
    def _write(self, playlist: Playlist) -> None:
        try:
            atomic_write_json(self._file(playlist.id), playlist.to_dict())
        except OSError as exc:  # pragma: no cover - disk full etc.
            log.error("could not save playlist %s: %s", playlist.name, exc)

    def save(self, playlist_id: str) -> bool:
        playlist = self._cache.get(playlist_id)
        if playlist is None:
            return False
        self._write(playlist)
        return True

    # -- import / export -------------------------------------------------
    def export_json(self, playlist_id: str, target: Path) -> Path:
        playlist = self._cache[playlist_id]
        payload = playlist.to_dict()
        payload["exported"] = utcnow()
        atomic_write_json(target, payload, backup=False)
        return target

    def export_m3u(self, playlist_id: str, target: Path) -> Path:
        from tide.store.schema import atomic_write_text

        playlist = self._cache[playlist_id]
        lines = ["#EXTM3U"]
        for track in playlist.tracks:
            duration = track.duration if track.duration is not None else -1
            label = track.title or track.source
            lines.append(f"#EXTINF:{duration},{track.channel} - {label}")
            lines.append(track.source)
        atomic_write_text(target, "\n".join(lines) + "\n", backup=False)
        return target

    def import_m3u(self, source: Path) -> Playlist:
        from tide.store.schema import atomic_write_text  # noqa: F401  (ensures module loaded)

        text = source.read_text(encoding="utf-8", errors="replace")
        tracks: list[Track] = []
        pending_duration: int | None = None
        pending_label = ""
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            if line.startswith("#EXTINF:"):
                meta = line[len("#EXTINF:"):]
                duration_text, _, label = meta.partition(",")
                try:
                    pending_duration = int(float(duration_text))
                except ValueError:
                    pending_duration = None
                pending_label = label.strip()
                continue
            if line.startswith("#"):
                continue
            title = pending_label
            channel = ""
            if " - " in pending_label:
                channel, _, title = pending_label.partition(" - ")
            tracks.append(
                Track(source=line, title=title.strip(), channel=channel.strip(), duration=pending_duration)
            )
            pending_duration = None
            pending_label = ""
        name = source.stem or "Imported"
        playlist = self.create(name)
        self.set_tracks(playlist.id, tracks)
        return self._cache[playlist.id]
