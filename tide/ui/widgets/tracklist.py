"""The center list: search results, queue, library, playlist contents.

Rows are built as Rich ``Text`` (never markup strings) so titles containing
brackets or backslashes cannot break rendering, and every number uses fixed
tabular columns so selection never jitters.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.events import Resize
from textual.widgets import OptionList
from textual.widgets.option_list import Option

from tide.store.playlists import Track
from tide.textutil import fit, format_duration, tabular
from tide.visual.ambient import ThemeColors

DUR_WIDTH = 8
MARKER_WIDTH = 2
GAP = 2


@dataclass(slots=True)
class ListRow:
    """One selectable line bound to a playable track."""

    track: Track
    kind: str = "result"  # result | queue | library | playlist


class TrackList(OptionList):
    """Virtualized row list with fixed-width columns and a current marker."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.rows: list[ListRow] = []
        self.current_source: str = ""
        self._colors: ThemeColors | None = None
        self._built_width: int = -1
        self._built_count: int = -1

    # -- data ------------------------------------------------------------
    def set_rows(self, rows: list[ListRow], current_source: str = "") -> None:
        self.rows = list(rows)
        self.current_source = current_source
        self._rebuild(force=True)

    def set_current(self, source: str) -> None:
        if source == self.current_source:
            return
        self.current_source = source
        self._rebuild(force=True)

    def set_colors(self, colors: ThemeColors) -> None:
        if colors == self._colors:
            return
        self._colors = colors
        self._rebuild(force=True)

    @property
    def selected_row(self) -> ListRow | None:
        index = self.highlighted
        if index is None or not (0 <= index < len(self.rows)):
            return None
        return self.rows[index]

    def select_index(self, index: int) -> None:
        if self.rows:
            self.highlighted = max(0, min(index, len(self.rows) - 1))

    # -- rendering -------------------------------------------------------
    def on_resize(self, event: Resize) -> None:
        # Note: OptionList implements _on_resize; our handler runs alongside it.
        self._rebuild(force=False)

    def _rebuild(self, *, force: bool) -> None:
        width = self.size.width or 80
        width = max(40, width)
        if not force and width == self._built_width and len(self.rows) == self._built_count:
            return
        previous = self.highlighted
        self._built_width = width
        self._built_count = len(self.rows)
        self.clear_options()
        if not self.rows:
            return
        options = [
            Option(self._row_text(index, row, width), id=f"row-{index}")
            for index, row in enumerate(self.rows)
        ]
        self.add_options(options)
        if previous is not None and previous < len(self.rows):
            self.highlighted = previous
        elif self.highlighted is None:
            self.highlighted = 0

    def _row_text(self, index: int, row: ListRow, width: int) -> Text:
        colors = self._colors
        foreground = colors.foreground if colors else ""
        dim = colors.dim if colors else "dim"
        faint = colors.faint if colors else "dim"
        accent = colors.accent if colors else "bold"

        detail_width = max(8, min(26, width // 3))
        title_width = max(10, width - MARKER_WIDTH - GAP - detail_width - GAP - DUR_WIDTH)
        track = row.track
        current = bool(self.current_source) and track.source == self.current_source

        text = Text()
        text.append("> " if current else "  ", style=f"bold {accent}" if current else dim)
        text.append(
            fit(track.title or track.source, title_width),
            style=f"bold {accent}" if current else (foreground or ""),
        )
        text.append("  " * 1, style=dim)
        text.append(fit(track.channel or "", detail_width), style=faint)
        text.append("  ", style=dim)
        text.append(tabular(format_duration(track.duration), DUR_WIDTH), style=dim)
        return text
