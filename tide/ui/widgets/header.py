"""Header: app mark, view tabs, transport state, and the search input."""

from __future__ import annotations

from rich.text import Text
from textual.containers import Horizontal, Vertical
from textual.widgets import Input, Static

from tide.textutil import fit
from tide.visual.ambient import ThemeColors

VIEWS: tuple[tuple[str, str], ...] = (
    ("search", "Search"),
    ("queue", "Queue"),
    ("library", "Library"),
)

STATE_TEXT: dict[str, str] = {
    "idle": "stopped",
    "resolving": "loading",
    "buffering": "buffering",
    "playing": "playing",
    "paused": "paused",
    "seeking": "seeking",
    "ended": "ended",
    "error": "error",
    "stopped": "stopped",
}


class Header(Vertical):
    """Two rows: tabs + transport state, then the search field."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.view = "search"
        self.state = "idle"
        self._colors: ThemeColors | None = None

    def compose(self):
        with Horizontal(id="header-row"):
            yield Static("TIDE", id="appmark")
            yield Static(id="tabs")
            yield Static("stopped", id="transport-state")
        yield Input(
            placeholder="Search  (enter to run, up/down for history)",
            id="search",
        )

    # -- updates ---------------------------------------------------------
    def set_colors(self, colors: ThemeColors) -> None:
        if colors == self._colors:
            return
        self._colors = colors
        self.refresh_tabs()
        state = self.query_one("#transport-state", Static)
        state.update(fit(STATE_TEXT.get(self.state, self.state), 12))

    def set_view(self, view: str) -> None:
        self.view = view
        self.refresh_tabs()

    def set_state(self, state: str) -> None:
        state = state.lower()
        if state == self.state:
            return
        self.state = state
        colors = self._colors
        style = f"bold {colors.accent}" if colors and state == "playing" else (
            colors.dim if colors else "dim"
        )
        widget = self.query_one("#transport-state", Static)
        widget.update(Text(fit(STATE_TEXT.get(state, state), 12), style=style))

    def refresh_tabs(self) -> None:
        colors = self._colors
        accent = colors.accent if colors else "bold"
        dim = colors.dim if colors else "dim"
        foreground = colors.foreground if colors else ""
        text = Text()
        for index, (key, label) in enumerate(VIEWS):
            if index:
                text.append("  ")
            active = key == self.view
            if active:
                text.append(f" {label} ", style=f"bold {accent}")
            else:
                text.append(f" {label} ", style=dim or foreground)
        self.query_one("#tabs", Static).update(text)
