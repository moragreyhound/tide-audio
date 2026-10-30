"""Status bar: transient messages on the left, stable hints on the right."""

from __future__ import annotations

import time

from rich.text import Text
from textual.containers import Horizontal
from textual.widgets import Static

from tide.visual.ambient import ThemeColors

DEFAULT_HINT = "f1 Help"


class StatusBar(Horizontal):
    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._colors: ThemeColors | None = None
        self._message: str = ""
        self._kind = "dim"
        self._expires: float | None = None
        self._counts: str = ""

    def compose(self):
        yield Static("", id="status-message")
        yield Static(DEFAULT_HINT, id="status-hint")

    def set_colors(self, colors: ThemeColors) -> None:
        if colors == self._colors:
            return
        self._colors = colors
        self._paint_message()
        self._paint_hint()

    def set_message(self, message: str, *, kind: str = "dim", ttl: float | None = 4.0) -> None:
        """Show ``message``; it reverts to the counts line after ``ttl`` seconds."""
        self._message = message
        self._kind = kind
        self._expires = None if ttl is None else (self._now + ttl)
        self._paint_message()

    def set_counts(self, counts: str) -> None:
        if counts == self._counts:
            return
        self._counts = counts
        self._paint_hint()

    def tick(self, now: float) -> None:
        if self._expires is not None and now >= self._expires:
            self._message = ""
            self._expires = None
            self._paint_message()

    @property
    def _now(self) -> float:
        return time.monotonic()

    def _paint_message(self) -> None:
        colors = self._colors
        if not self._message:
            style = colors.faint if colors else "dim"
            self.query_one("#status-message", Static).update(Text("", style=style))
            return
        if self._kind == "danger":
            style = colors.danger if colors else "red"
        elif self._kind == "ok":
            style = colors.ok if colors else "green"
        elif self._kind == "accent":
            style = colors.accent if colors else ""
        else:
            style = colors.dim if colors else "dim"
        self.query_one("#status-message", Static).update(
            Text(self._message, style=style or "dim")
        )

    def _paint_hint(self) -> None:
        colors = self._colors
        style = colors.faint if colors else "dim"
        right = self._counts or ""
        hint = f"{right}   {DEFAULT_HINT}" if right else DEFAULT_HINT
        self.query_one("#status-hint", Static).update(Text(hint, style=style))
