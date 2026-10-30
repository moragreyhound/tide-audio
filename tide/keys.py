"""Key bindings: defaults, user overrides from ``keys.toml``, context routing.

Keys are normalized exactly like Textual does for incoming events
(``textual.keys._character_to_key``): single punctuation characters become
their unicode names (``+`` -> ``plus``, ``[`` -> ``left_square_bracket``,
``/`` -> ``slash``), letters keep their case (``D`` is not ``d``), and named
or modified keys are lowercased (``Shift+Left`` -> ``shift+left``). User
overrides written as ``shift+<letter>`` additionally register the uppercase
letter, because terminals report shift+letter as the uppercase character.

Actions belong to a group (context). Two actions only conflict when they
share a group - the same key may legitimately do different things in the
results list and in the transport (for example ``p`` = add to playlist while
browsing, previous track while the transport has focus). The app routes keys
through :meth:`Keymap.action_for` with the focused context first.

```toml
[bindings]
play_pause = "k"
row_queue = ["a", "ctrl+enter"]
```
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

import tomllib

from tide.paths import Paths, get_paths

# action -> (keys, label, group). More than one key means "any of these".
ACTIONS: dict[str, tuple[tuple[str, ...], str, str]] = {
    # -- global ----------------------------------------------------------
    "quit": (("q",), "Quit", "global"),
    "help": (("f1",), "Help", "global"),
    "focus_next": (("tab",), "Cycle focus", "global"),
    "toggle_rail": (("ctrl+b",), "Show / hide rail", "global"),
    "open_search": (("/",), "Search", "global"),
    "view_queue": (("1",), "Queue view", "global"),
    "view_library": (("2",), "Library view", "global"),
    "open_url": (("o",), "Open URL", "global"),
    "toggle_downloads": (("D",), "Downloads", "global"),
    "debug_overlay": (("f12",), "Debug overlay", "global"),
    "plain_snapshot": (("f11",), "Print status line", "global"),
    "playlist_new": (("P",), "New playlist", "global"),
    "playlist_rename": (("R",), "Rename playlist", "global"),
    "playlist_delete": (("delete",), "Delete playlist", "global"),
    "playlist_confirm_delete": (("y",), "Confirm delete", "global"),
    "playlist_import": (("I",), "Import playlist", "global"),
    "playlist_export": (("E",), "Export playlist", "global"),
    # -- transport -------------------------------------------------------
    "play_pause": (("space",), "Play / pause", "transport"),
    "next_track": (("n",), "Next", "transport"),
    "prev_track": (("p",), "Previous", "transport"),
    "seek_back": (("left",), "-5s", "transport"),
    "seek_fwd": (("right",), "+5s", "transport"),
    "seek_back_big": (("shift+left",), "-30s", "transport"),
    "seek_fwd_big": (("shift+right",), "+30s", "transport"),
    "seek_back_pct": (("ctrl+left",), "-1%", "transport"),
    "seek_fwd_pct": (("ctrl+right",), "+1%", "transport"),
    "volume_up": (("+",), "Volume up", "transport"),
    "volume_down": (("-",), "Volume down", "transport"),
    "mute": (("m",), "Mute", "transport"),
    "shuffle": (("s",), "Shuffle", "transport"),
    "repeat": (("r",), "Repeat", "transport"),
    "retry": (("ctrl+r",), "Retry", "transport"),
    "speed_down": (("[",), "Slower", "transport"),
    "speed_up": (("]",), "Faster", "transport"),
    "equalizer": (("e",), "Equalizer", "transport"),
    "viz_cycle": (("v",), "Visualizer", "transport"),
    # -- results / list views (search, queue, playlist contents) ---------
    "row_play": (("enter",), "Play now", "results"),
    "row_queue": (("a", "shift+enter"), "Add to queue", "results"),
    "row_playlist": (("p",), "Add to playlist", "results"),
    "row_download": (("d",), "Download", "results"),
    "row_move_up": (("alt+up",), "Move up", "results"),
    "row_move_down": (("alt+down",), "Move down", "results"),
    # -- rail (playlist management) --------------------------------------
    "playlist_play": (("enter",), "Play playlist", "rail"),
    "playlist_shuffle_play": (("shift+enter", "S"), "Shuffle play", "rail"),
    "playlist_inspect": (("space",), "Open playlist", "rail"),
    # -- downloads panel -------------------------------------------------
    "download_retry": (("r",), "Retry", "downloads"),
    "download_cancel": (("x",), "Cancel", "downloads"),
    # -- overlays (search box, pickers) ----------------------------------
    "overlay_close": (("escape",), "Close", "overlay"),
    "history_prev": (("up",), "Previous query", "overlay"),
    "history_next": (("down",), "Next query", "overlay"),
}

# Groups whose keys take precedence while one of their widgets is focused.
CONTEXT_GROUPS = ("overlay", "results", "rail", "downloads")
# Groups that are always routable when no context claims the key.
BASE_GROUPS = ("global", "transport")

# Friendly display names for the help overlay and footer.
_KEY_DISPLAY = {
    "left": "←",
    "right": "→",
    "up": "↑",
    "down": "↓",
    "enter": "Enter",
    "shift+enter": "Shift+Enter",
    "escape": "Esc",
    "tab": "Tab",
    "space": "Space",
    "delete": "Del",
    "backspace": "Backspace",
    "pageup": "PgUp",
    "pagedown": "PgDn",
    "home": "Home",
    "end": "End",
}

_REPLACEMENTS = {
    "solidus": "slash",
    "reverse_solidus": "backslash",
    "commercial_at": "at",
    "hyphen_minus": "minus",
    "plus_sign": "plus",
    "low_line": "underscore",
}

_NAMED_KEYS = {
    "left", "right", "up", "down", "enter", "escape", "tab", "space",
    "backspace", "delete", "insert", "home", "end", "pageup", "pagedown",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
    "newline", "shift+tab", "shift+escape",
}


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------
def normalize_key(key: str) -> str:
    """Normalize a binding key the same way Textual normalizes event keys."""
    text = key.strip()
    if not text:
        return ""
    if len(text) == 1:
        if text.isalnum() or text in "_":
            return text
        if text == "\t":
            return "tab"
        try:
            name = unicodedata.name(text).lower().replace("-", "_").replace(" ", "_")
        except ValueError:
            return text
        return _REPLACEMENTS.get(name, name)
    if "+" in text:
        parts = [p for p in text.split("+")]
        *mods, last = parts
        if not all(m in ("ctrl", "shift", "alt", "meta") for m in mods):
            return text.lower()
        normalized_last = normalize_key(last)
        return "+".join([*mods, normalized_last])
    return text.lower()


def expand_aliases(key: str) -> tuple[str, ...]:
    """Extra equivalent keys (terminals report shift+letter as uppercase)."""
    normalized = normalize_key(key)
    if normalized.startswith("shift+") and len(normalized) == 7:
        letter = normalized[-1]
        if letter.isalnum():
            return (normalized, letter.upper())
    return (normalized,)


def pretty_key(key: str) -> str:
    """Human-readable form for the help table and footer."""
    normalized = normalize_key(key)
    if normalized in _KEY_DISPLAY:
        return _KEY_DISPLAY[normalized]
    if "+" in normalized:
        *mods, last = normalized.split("+")
        labels = {"ctrl": "Ctrl", "shift": "Shift", "alt": "Alt", "meta": "Meta"}
        head = "+".join(labels.get(m, m.capitalize()) for m in mods)
        tail = last.upper() if len(last) == 1 else _KEY_DISPLAY.get(last, last.capitalize())
        if len(last) == 1 and last in _REPLACEMENTS.values():
            tail = last  # plus, minus, slash read better as symbols
        return f"{head}+{tail}"
    if len(normalized) == 1:
        return normalized.upper() if normalized.isalpha() else normalized
    return _KEY_DISPLAY.get(normalized, normalized.capitalize())


def validate_key(key: str) -> bool:
    """Syntactic check before normalization (used for user overrides)."""
    if not isinstance(key, str) or not key.strip():
        return False
    text = key.strip()
    if len(text) == 1:
        return text.isprintable()
    if "+" in text:
        *mods, last = text.split("+")
        if not mods or not all(m.lower() in ("ctrl", "shift", "alt", "meta") for m in mods):
            return False
        return bool(last) and (len(last) == 1 and last.isprintable() or last.lower() in _NAMED_KEYS)
    return text.lower() in _NAMED_KEYS or (len(text) == 1 and text.isprintable())


# ---------------------------------------------------------------------------
# Keymap
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class KeyIssue:
    key: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.key}: {self.message}"


class Keymap:
    """Resolved bindings with per-context lookups."""

    def __init__(self, mapping: dict[str, tuple[str, ...]], issues: list[KeyIssue]) -> None:
        self._action_keys = {a: tuple(k) for a, k in mapping.items()}
        self.issues = issues
        # key -> action per group (first declared wins within a group)
        self._by_group: dict[str, dict[str, str]] = {}
        for action, (keys, _label, group) in ACTIONS.items():
            table = self._by_group.setdefault(group, {})
            for key in self._action_keys.get(action, ()):
                table.setdefault(key, action)
        # always-available map: global + transport
        self._base: dict[str, str] = {}
        for group in BASE_GROUPS:
            for key, action in self._by_group.get(group, {}).items():
                self._base.setdefault(key, action)

    # -- queries ---------------------------------------------------------
    def keys_for(self, action: str) -> tuple[str, ...]:
        return self._action_keys.get(action, ())

    def display_for(self, action: str) -> str:
        keys = self.keys_for(action)
        return ", ".join(pretty_key(k) for k in keys)

    def label(self, action: str) -> str:
        return ACTIONS.get(action, ((), action, "global"))[1]

    def action_for(self, key: str, context: str | None = None) -> str | None:
        """Route a normalized event key: context first, then global/transport."""
        if context:
            action = self._by_group.get(context, {}).get(key)
            if action:
                return action
        return self._base.get(key)

    def group_table(self, group: str) -> dict[str, str]:
        return dict(self._by_group.get(group, {}))

    def help_groups(self) -> list[tuple[str, list[tuple[str, str, str]]]]:
        """``(group_label, [(keys, label, action)])`` sorted for the help view."""
        titles = {
            "global": "Global",
            "transport": "Transport",
            "results": "Lists",
            "rail": "Playlists",
            "downloads": "Downloads",
            "overlay": "Overlays",
        }
        out: list[tuple[str, list[tuple[str, str, str]]]] = []
        for group in ("global", "transport", "results", "rail", "downloads", "overlay"):
            rows = [
                (self.display_for(action), self.label(action), action)
                for action in ACTIONS
                if ACTIONS[action][2] == group
            ]
            if rows:
                out.append((titles[group], rows))
        return out

    def all_keys(self) -> list[str]:
        keys: list[str] = []
        for table in self._by_group.values():
            keys.extend(table)
        return keys


def load_keymap(paths: Paths | None = None) -> Keymap:
    """Build the keymap from defaults plus optional ``keys.toml`` overrides."""
    paths = paths or get_paths()
    issues: list[KeyIssue] = []
    # action -> normalized keys
    mapping: dict[str, tuple[str, ...]] = {
        action: tuple(dict.fromkeys(k for key in keys for k in expand_aliases(key)))
        for action, (keys, _, _) in ACTIONS.items()
    }

    user_file = paths.keys_file
    if user_file.is_file():
        try:
            with user_file.open("rb") as fh:
                data = tomllib.load(fh)
        except (tomllib.TOMLDecodeError, OSError) as exc:
            issues.append(KeyIssue("<file>", f"could not read keys.toml ({exc}); using defaults"))
            data = {}
        overrides = data.get("bindings", {})
        if not isinstance(overrides, dict):
            issues.append(KeyIssue("bindings", 'expected a table of action = "key" pairs'))
            overrides = {}
        for action, value in overrides.items():
            if action not in ACTIONS:
                issues.append(KeyIssue(action, "unknown action (ignored)"))
                continue
            raw_keys = [value] if isinstance(value, str) else list(value) if isinstance(value, list) else []
            if not raw_keys or not all(isinstance(k, str) and validate_key(k) for k in raw_keys):
                issues.append(
                    KeyIssue(
                        action,
                        f"invalid key {value!r}; keeping default "
                        f"{', '.join(ACTIONS[action][0])!r}",
                    )
                )
                continue
            mapping[action] = tuple(
                dict.fromkeys(k for key in raw_keys for k in expand_aliases(key))
            )
        for extra in data:
            if extra != "bindings":
                issues.append(KeyIssue(extra, "unknown section (ignored)"))

    # Conflict detection: only within a group. First declared action wins;
    # later actions lose the clashing key and keep whatever remains.
    defaults: dict[str, tuple[str, ...]] = {
        action: tuple(dict.fromkeys(k for key in keys for k in expand_aliases(key)))
        for action, (keys, _, _) in ACTIONS.items()
    }
    for group in {spec[2] for spec in ACTIONS.values()}:
        claimed: dict[str, str] = {}
        for action, (_keys, _label, act_group) in ACTIONS.items():
            if act_group != group:
                continue
            kept: list[str] = []
            dropped: list[str] = []
            for key in mapping[action]:
                holder = claimed.get(key)
                if holder is not None and holder != action:
                    dropped.append(key)
                    continue
                claimed[key] = action
                kept.append(key)
            if dropped:
                issues.append(
                    KeyIssue(
                        action,
                        f"key {', '.join(pretty_key(k) for k in dropped)} already bound in "
                        f"group '{group}'; '{action}' keeps "
                        f"{', '.join(pretty_key(k) for k in kept) or 'no key'}",
                    )
                )
            if kept:
                mapping[action] = tuple(kept)
            else:
                # Every override was taken: restore the default keys (shadowed
                # lookups resolve to the earlier action, but nothing crashes).
                for key in defaults[action]:
                    claimed.setdefault(key, action)
                mapping[action] = defaults[action]

    return Keymap(mapping, issues)
