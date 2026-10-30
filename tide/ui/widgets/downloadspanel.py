"""Downloads panel: queue state, progress, speed, and per-item actions.

Toggled with ``D``. The panel is an OptionList so it can take focus and
claim the ``downloads`` key context (``r`` retry, ``x`` cancel).
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from tide.media.download import DownloadItem, DownloadState
from tide.textutil import abbrev_count, fit, format_duration, tabular
from tide.visual.ambient import ThemeColors

STATE_LABEL: dict[DownloadState, str] = {
    DownloadState.QUEUED: "queued",
    DownloadState.DOWNLOADING: "downloading",
    DownloadState.CONVERTING: "converting",
    DownloadState.DONE: "done",
    DownloadState.FAILED: "failed",
    DownloadState.CANCELED: "canceled",
}

ROW_WIDTH = 46
BAR_WIDTH = 10


class DownloadsPanel(OptionList):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.items: list[DownloadItem] = []
        self._colors: ThemeColors | None = None
        self._signature: tuple = ()

    def set_items(self, items: list[DownloadItem]) -> None:
        """Repaint only when something visible changed (app throttles calls)."""
        signature = tuple(
            (i.id, i.state, round(i.progress, 2), i.message[:24], i.speed or 0) for i in items
        )
        if signature == self._signature:
            return
        self._signature = signature
        self.items = list(items)
        previous = self.highlighted
        self.clear_options()
        if not self.items:
            self.add_option(Option(Text("No downloads.", style=self._dim()), id="dl-empty"))
            return
        options = [
            Option(self._row_text(item), id=f"dl-{item.id}") for item in self.items
        ]
        self.add_options(options)
        if previous is not None and previous < len(self.items):
            self.highlighted = previous
        elif self.highlighted is None:
            self.highlighted = 0

    @property
    def selected_item_id(self) -> str | None:
        index = self.highlighted
        if index is None or not (0 <= index < len(self.items)):
            return None
        return self.items[index].id

    def set_colors(self, colors: ThemeColors) -> None:
        if colors == self._colors:
            return
        self._colors = colors
        self._signature = ()  # force repaint
        self.set_items(self.items)

    # -- row text --------------------------------------------------------
    def _dim(self) -> str:
        return self._colors.dim if self._colors else "dim"

    def _row_text(self, item: DownloadItem) -> Text:
        colors = self._colors
        foreground = colors.foreground if colors else ""
        dim = colors.dim if colors else "dim"
        faint = colors.faint if colors else "dim"
        ok = colors.ok if colors else "green"
        danger = colors.danger if colors else "red"
        accent = colors.accent if colors else ""

        state = item.state
        state_style = dim
        if state == DownloadState.DONE:
            state_style = ok
        elif state == DownloadState.FAILED:
            state_style = danger
        elif state in (DownloadState.DOWNLOADING, DownloadState.CONVERTING):
            state_style = accent or foreground

        text = Text()
        text.append(fit(STATE_LABEL[state], 11), style=state_style or dim)
        text.append(" ", style=dim)
        title_width = ROW_WIDTH - 11 - 1 - BAR_WIDTH - 1 - 7
        text.append(fit(item.title, max(8, title_width)), style=foreground or "")
        text.append(" ", style=dim)
        if state in (DownloadState.DOWNLOADING, DownloadState.CONVERTING):
            fill = int(item.progress * BAR_WIDTH)
            text.append("━" * fill, style=state_style or "")
            text.append("─" * (BAR_WIDTH - fill), style=faint)
            text.append(" ", style=dim)
            text.append(tabular(f"{round(item.progress * 100):>3d}%", 5), style=dim)
        elif state == DownloadState.FAILED:
            text.append(fit(item.message or "failed", BAR_WIDTH + 7), style=danger)
        elif state == DownloadState.DONE:
            text.append("complete" if not item.target else fit(_basename(item.target), BAR_WIDTH + 7), style=ok)
        elif state == DownloadState.CANCELED:
            text.append("canceled", style=faint)
        else:
            text.append("waiting", style=faint)
        return text


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


__all__ = ["DownloadsPanel", "STATE_LABEL", "abbrev_count", "format_duration"]
