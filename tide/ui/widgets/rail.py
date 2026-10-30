"""Left rail: the playlist browser (collapsible with ctrl+b)."""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text
from textual.events import Resize
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from tide.textutil import fit, tabular
from tide.visual.ambient import ThemeColors


@dataclass(frozen=True, slots=True)
class RailEntry:
    playlist_id: str
    name: str
    count: int


class Rail(OptionList):
    """Playlist names with track counts; selection is reported as events."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.entries: list[RailEntry] = []
        self._colors: ThemeColors | None = None
        self._built_width: int = -1
        self._selected_id: str | None = None

    def set_entries(self, entries: list[RailEntry]) -> None:
        self.entries = list(entries)
        self._rebuild(force=True)

    def set_colors(self, colors: ThemeColors) -> None:
        if colors == self._colors:
            return
        self._colors = colors
        self._rebuild(force=True)

    @property
    def selected_playlist_id(self) -> str | None:
        index = self.highlighted
        if index is None or not (0 <= index < len(self.entries)):
            return self._selected_id
        return self.entries[index].playlist_id

    def highlight_playlist(self, playlist_id: str) -> None:
        self._selected_id = playlist_id
        for index, entry in enumerate(self.entries):
            if entry.playlist_id == playlist_id:
                self.highlighted = index
                return

    def on_resize(self, event: Resize) -> None:
        # Note: OptionList implements _on_resize; our handler runs alongside it.
        self._rebuild(force=False)

    def _rebuild(self, *, force: bool) -> None:
        width = max(14, self.size.width or 22)
        if not force and width == self._built_width:
            return
        previous = self.highlighted
        self._built_width = width
        self.clear_options()
        if not self.entries:
            return
        options = [
            Option(self._entry_text(entry, width), id=f"rail-{entry.playlist_id}")
            for entry in self.entries
        ]
        self.add_options(options)
        if previous is not None and previous < len(self.entries):
            self.highlighted = previous
        elif self.highlighted is None:
            self.highlighted = 0

    def _entry_text(self, entry: RailEntry, width: int) -> Text:
        colors = self._colors
        foreground = colors.foreground if colors else ""
        dim = colors.dim if colors else "dim"
        faint = colors.faint if colors else "dim"
        count_width = 4
        name_width = max(6, width - count_width - 1)
        text = Text()
        text.append(fit(entry.name, name_width), style=foreground or "")
        text.append(" ", style=dim)
        text.append(tabular(str(entry.count), count_width), style=faint)
        return text


class RailHeader(Static):
    """Small caption above the rail."""

    def set_colors(self, colors: ThemeColors | None) -> None:
        style = colors.faint if colors else "dim"
        self.update(Text("PLAYLISTS", style=style or "dim"))
