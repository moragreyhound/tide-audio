"""Modal overlays: help table, single-question prompts, option pickers.

Every overlay returns a value through ``push_screen(screen, callback)``:
``None`` always means "closed / no action", so callers never guess.
"""

from __future__ import annotations

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.containers import Container, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from tide.keys import Keymap, normalize_key, pretty_key
from tide.visual.ambient import ThemeColors


class HelpScreen(ModalScreen[None]):
    """Key reference built straight from the keymap (never hard-coded)."""

    BINDINGS = [("escape", "close"), ("f1", "close"), ("q", "close")]

    def __init__(self, keymap: Keymap, theme_colors: ThemeColors | None = None) -> None:
        super().__init__()
        self.keymap = keymap
        self.theme_colors = theme_colors

    def compose(self) -> ComposeResult:
        body = Text()
        for group_label, rows in self.keymap.help_groups():
            body.append(f"{group_label}\n", style="bold underline")
            for keys, label, _action in rows:
                body.append(f"  {keys:<16}", style="")
                body.append(f"{label}\n", style="dim")
            body.append("\n")
        with Container(id="help-box"):
            yield Static("Keys", id="help-title")
            with VerticalScroll(id="help-scroll"):
                yield Static(body)

    def action_close(self) -> None:
        self.dismiss(None)


class PromptScreen(ModalScreen[str | None]):
    """One question: either a choice of keys or a text input.

    ``choices`` are ``(key, label)`` pairs; the normalized pressed key is
    returned. With ``input_label`` set, the typed string is returned instead.
    """

    def __init__(
        self,
        message: str,
        *,
        title: str = "",
        choices: list[tuple[str, str]] | None = None,
        input_label: str | None = None,
        initial: str = "",
        theme_colors: ThemeColors | None = None,
    ) -> None:
        super().__init__()
        self.prompt_message = message
        self.title_text = title or "Confirm"
        self.choices = [(normalize_key(key), label) for key, label in (choices or [])]
        self.input_label = input_label
        self.initial = initial
        self.theme_colors = theme_colors

    def compose(self) -> ComposeResult:
        with Container(id="prompt-box"):
            yield Static(self.title_text, id="prompt-title")
            yield Static(self.prompt_message, id="prompt-message")
            if self.input_label is not None:
                yield Input(value=self.initial, placeholder=self.input_label, id="prompt-input")
            if self.choices:
                text = Text()
                for key, label in self.choices:
                    text.append(f"[{pretty_key(key)}] ", style="bold")
                    text.append(f"{label}   ", style="dim")
                yield Static(text, id="prompt-choices")

    def on_mount(self) -> None:
        if self.input_label is not None:
            self.query_one("#prompt-input", Input).focus()

    def on_key(self, event: events.Key) -> None:
        if self.input_label is not None:
            if event.key == "escape":
                event.stop()
                self.dismiss(None)
            return
        key = normalize_key(event.key)
        if key == "escape":
            event.stop()
            self.dismiss(None)
            return
        for choice_key, _label in self.choices:
            if key == choice_key:
                event.stop()
                self.dismiss(choice_key)
                return

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value.strip()
        self.dismiss(value or None)


class PickerScreen(ModalScreen[str | None]):
    """Pick one item from a list (playlists, presets, recent queries)."""

    def __init__(
        self,
        title: str,
        options: list[str],
        *,
        theme_colors: ThemeColors | None = None,
    ) -> None:
        super().__init__()
        self.title_text = title
        self.options = list(options)
        self.theme_colors = theme_colors

    def compose(self) -> ComposeResult:
        with Container(id="picker-box"):
            yield Static(self.title_text, id="picker-title")
            picker = OptionList(id="picker-list")
            for index, option in enumerate(self.options):
                picker.add_option(Option(option, id=f"pick-{index}"))
            yield picker

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        index = event.option_index
        if 0 <= index < len(self.options):
            self.dismiss(self.options[index])
        else:
            self.dismiss(None)

    def on_key(self, event: events.Key) -> None:
        if normalize_key(event.key) == "escape":
            event.stop()
            self.dismiss(None)
