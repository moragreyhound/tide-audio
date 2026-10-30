"""Search: ``ytsearchN:`` flat extraction with caching and history-friendly
result records.

Results are plain data - no expiring URLs (flat entries usually have none,
and we never persist them anyway).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from tide.log import get_logger
from tide.media.ydl import apply_network, base_opts, open_ydl, with_retries
from tide.textutil import format_duration

log = get_logger(__name__)

CACHE_SIZE = 5


@dataclass(frozen=True)
class SearchResult:
    source: str            # stable watch URL (or raw id resolved to one)
    title: str
    channel: str
    duration: int | None
    views: int | None
    thumbnail: str | None
    extractor: str = ""

    @property
    def duration_text(self) -> str:
        return format_duration(self.duration)

    @property
    def id(self) -> str:
        if "v=" in self.source:
            return self.source.split("v=", 1)[1].split("&", 1)[0]
        return self.source

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "title": self.title,
            "channel": self.channel,
            "duration": self.duration,
            "views": self.views,
            "thumbnail": self.thumbnail,
        }


class SearchCache:
    """Keeps the last five result sets so revisiting a query is instant."""

    def __init__(self, size: int = CACHE_SIZE) -> None:
        self.size = size
        self._entries: dict[str, list[SearchResult]] = {}

    def get(self, key: str) -> list[SearchResult] | None:
        if key in self._entries:
            # refresh recency
            value = self._entries.pop(key)
            self._entries[key] = value
            return list(value)
        return None

    def put(self, key: str, value: list[SearchResult]) -> None:
        self._entries.pop(key, None)
        self._entries[key] = list(value)
        while len(self._entries) > self.size:
            self._entries.pop(next(iter(self._entries)))

    def clear(self) -> None:
        self._entries.clear()


def _watch_url(entry: dict[str, Any]) -> str | None:
    """Stable source URL from a flat entry."""
    url = entry.get("url") or entry.get("webpage_url")
    video_id = entry.get("id")
    ie_key = (entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if isinstance(url, str) and url.startswith(("http://", "https://")):
        # Flat playlist entries sometimes hand back a bare id in ``url``.
        if video_id and url == video_id:
            url = None
        else:
            return url
    if video_id and (not ie_key or "youtube" in ie_key or ie_key == "youtube"):
        return f"https://www.youtube.com/watch?v={video_id}"
    if isinstance(url, str) and url:
        return url
    return None


def parse_entries(payload: dict[str, Any]) -> list[SearchResult]:
    """Convert an ``ytsearchN:`` result document into :class:`SearchResult`s.

    Tolerant of missing fields: age-restricted or region-blocked entries that
    still appear in the flat list are kept with whatever metadata exists.
    """
    entries = payload.get("entries")
    out: list[SearchResult] = []
    if not isinstance(entries, list):
        return out
    for raw in entries:
        if not isinstance(raw, dict):
            continue
        source = _watch_url(raw)
        if not source:
            continue
        title = str(raw.get("title") or "").strip()
        if not title:
            continue
        channel = str(raw.get("channel") or raw.get("uploader") or raw.get("uploader_id") or "")
        duration = raw.get("duration")
        if not isinstance(duration, (int, float)) or isinstance(duration, bool):
            duration = None
        views = raw.get("view_count")
        if not isinstance(views, (int, float)) or isinstance(views, bool):
            views = None
        thumbnails = raw.get("thumbnails")
        thumb = raw.get("thumbnail")
        if not thumb and isinstance(thumbnails, list) and thumbnails:
            from tide.media.thumbnails import best_thumbnail_url

            thumb = best_thumbnail_url(thumbnails)
        out.append(
            SearchResult(
                source=source,
                title=title,
                channel=channel.strip(),
                duration=int(duration) if duration is not None else None,
                views=int(views) if views is not None else None,
                thumbnail=str(thumb) if isinstance(thumb, str) else None,
                extractor=str(raw.get("ie_key") or raw.get("extractor_key") or ""),
            )
        )
    return out


def build_search_opts(count: int, config: Any = None) -> dict[str, Any]:
    count = max(1, min(int(count), 50))
    opts = base_opts(
        {
            "extract_flat": True,
            "playlistend": count,
            "ignoreerrors": True,  # one dead entry must not kill the search
        }
    )
    return apply_network(opts, config)


def search(
    query: str,
    *,
    count: int = 20,
    config: Any = None,
    cache: SearchCache | None = None,
    attempts: int = 3,
    ydl_factory: Callable[[dict[str, Any]], Any] | None = None,
) -> list[SearchResult]:
    """Run ``ytsearch<count>:query`` and return parsed results."""
    key = f"{count}:{query.strip().lower()}"
    if cache is not None:
        cached = cache.get(key)
        if cached is not None:
            log.debug("search cache hit for %r", query)
            return cached

    factory = ydl_factory or open_ydl
    opts = build_search_opts(count, config)
    term = f"ytsearch{count}:{query}"

    def run() -> list[SearchResult]:
        with factory(opts) as ydl:
            payload = ydl.extract_info(term, download=False)
        if not isinstance(payload, dict):
            return []
        return parse_entries(payload)

    results = with_retries(run, attempts=attempts)
    if cache is not None:
        cache.put(key, results)
    log.debug("search %r -> %d results", query, len(results))
    return results


def format_table(results: list[SearchResult], width: int = 100) -> list[str]:
    """Plain-text table for ``tide search`` (no Rich dependency)."""
    from tide.textutil import abbrev_count, fit

    rows = [f"{'#':>3}  {'TITLE':<44}  {'CHANNEL':<20}  {'TIME':>8}  {'VIEWS':>7}"]
    rows.append("-" * min(width, 90))
    for index, item in enumerate(results[:50], start=1):
        title = fit(item.title, 44)
        channel = fit(item.channel or "-", 20)
        time_text = item.duration_text if item.duration else "--:--"
        rows.append(
            f"{index:>3}  {title:<44}  {channel:<20}  {time_text:>8}  "
            f"{abbrev_count(item.views):>7}"
        )
    return rows
