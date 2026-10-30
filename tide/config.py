"""Configuration loading, validation and defaults.

The config file lives at ``<config_dir>/config.toml``. Loading is forgiving:

* a missing file is created with documented defaults on first run;
* an invalid value records an issue naming the key and the expected type, then
  falls back to the default **for that key only** - the app still starts;
* unknown keys are reported and ignored.

Environment overrides (applied after the file):

* ``TIDE_MUSIC_DIR``, ``TIDE_FFMPEG_PATH``, ``TIDE_COOKIEFILE`` - paths
* ``TIDE_MOTION``, ``TIDE_REDUCE_MOTION`` - motion settings
* ``TIDE_LOG_LEVEL`` - logging verbosity
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

import tomli_w

from tide.paths import Paths, ensure_dirs, find_cookiefile, get_paths

# ---------------------------------------------------------------------------
# Vocabulary
# ---------------------------------------------------------------------------
DOWNLOAD_FORMATS = ("mp3", "m4a", "opus", "flac", "wav")
AMBIENT_SOURCES = ("thumbnail", "video", "static", "off")
VISUALIZER_MODES = ("spectrum", "scope", "radial", "particles", "wash")
MOTIONS = ("full", "reduced", "none")
THEMES = ("auto", "dark", "light")
ON_EXISTING = ("ask", "skip", "overwrite")
BROWSERS = ("chrome", "firefox", "edge", "safari", "brave", "opera", "vivaldi", "chromium")
BITRATES = ("128", "192", "256", "320", "best")
LOG_LEVELS = ("debug", "info", "warning", "error", "critical")

DEFAULT_TEMPLATE = "%(artist,uploader)s/%(album,playlist_title|Singles)s/%(title)s.%(ext)s"


def _default_music_dir() -> Path:
    try:
        from platformdirs import user_music_dir

        return Path(user_music_dir(appauthor=False)).expanduser()
    except Exception:  # pragma: no cover - exotic platforms
        return Path.home() / "Music"


# ---------------------------------------------------------------------------
# Spec table
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Spec:
    kind: str  # bool | int | float | str | choice | path | hexcolor | browser
    default: Any
    desc: str
    choices: tuple[str, ...] = ()
    lo: float | None = None
    hi: float | None = None
    nullable: bool = True


SPECS: dict[str, Spec] = {
    "music_dir": Spec("path", _default_music_dir(), "Download destination directory."),
    "download_format": Spec(
        "choice", "mp3", "Audio format after conversion.", choices=DOWNLOAD_FORMATS, nullable=False
    ),
    "download_template": Spec(
        "str", DEFAULT_TEMPLATE, "yt-dlp output template (relative to music_dir).", nullable=False
    ),
    "download_concurrency": Spec(
        "int", 2, "Parallel downloads.", lo=1, hi=8, nullable=False
    ),
    "download_bitrate": Spec(
        "choice", "192", "Constant bitrate for lossy formats.", choices=BITRATES, nullable=False
    ),
    "on_exists": Spec(
        "choice",
        "ask",
        "What to do when the target file exists: ask once per session, skip, or overwrite.",
        choices=ON_EXISTING,
        nullable=False,
    ),
    "search_count": Spec("int", 20, "Results per search.", lo=1, hi=50, nullable=False),
    "ffmpeg_path": Spec("path", None, "Override the ffmpeg binary location."),
    "ambient_source": Spec(
        "choice",
        "thumbnail",
        "Where ambient colors come from: thumbnail, video, static, or off.",
        choices=AMBIENT_SOURCES,
        nullable=False,
    ),
    "ambient_video_interval": Spec(
        "float", 12.0, "Seconds between sampled frames when ambient_source = video.", lo=5.0, hi=60.0
    ),
    "visualizer_default": Spec(
        "choice", "wash", "Initial visualizer mode.", choices=VISUALIZER_MODES, nullable=False
    ),
    "motion": Spec(
        "choice", "full", "Animation level.", choices=MOTIONS, nullable=False
    ),
    "theme": Spec("choice", "auto", "Color theme.", choices=THEMES, nullable=False),
    "high_contrast": Spec("bool", False, "Use the high-contrast theme variant."),
    "accent_override": Spec("hexcolor", None, "Fixed accent color (#rrggbb) instead of artwork."),
    "restore_session": Spec("bool", True, "Save and restore playback state on exit."),
    "proxy": Spec("str", None, "Proxy URL passed to yt-dlp."),
    "cookies_from_browser": Spec(
        "browser", None, "Load cookies from this browser (privacy warning: it reads browser state)."
    ),
    "cookiefile": Spec(
        "path",
        None,
        "Netscape cookies.txt path. Auto-detected from ./cookies.txt when unset.",
    ),
    "log_level": Spec("choice", "info", "Log verbosity.", choices=LOG_LEVELS, nullable=False),
}


# ---------------------------------------------------------------------------
# Validation results
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConfigIssue:
    key: str
    message: str
    fatal: bool = False

    def __str__(self) -> str:  # pragma: no cover - display helper
        return f"{self.key}: {self.message}"


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _coerce(spec: Spec, value: Any) -> tuple[Any, str | None]:
    """Coerce ``value`` for ``spec``.

    Returns ``(coerced, error_message)``. When an error message is returned the
    caller falls back to the default for that key.
    """
    if value is None:
        if spec.nullable:
            return None, None
        return spec.default, f"expected {spec.desc.rstrip('.')} (key may not be empty/null)"

    # TOML has no null: optional keys are written as "" and read back as None.
    if spec.nullable and isinstance(value, str) and not value.strip():
        return None, None

    kind = spec.kind

    if kind == "bool":
        if isinstance(value, bool):
            return value, None
        if isinstance(value, str):
            low = value.strip().lower()
            if low in _TRUE:
                return True, None
            if low in _FALSE:
                return False, None
        return spec.default, "expected a boolean (true or false)"

    if kind == "int":
        if isinstance(value, bool):
            return spec.default, "expected an integer, got a boolean"
        if isinstance(value, int):
            num: float = float(value)
        elif isinstance(value, str):
            try:
                num = float(value.strip())
            except ValueError:
                return spec.default, "expected an integer"
            if num != int(num):
                return spec.default, "expected an integer"
        else:
            return spec.default, "expected an integer"
        if spec.lo is not None and num < spec.lo:
            return spec.default, f"expected an integer between {spec.lo:g} and {spec.hi:g}"
        if spec.hi is not None and num > spec.hi:
            return spec.default, f"expected an integer between {spec.lo:g} and {spec.hi:g}"
        return int(num), None

    if kind == "float":
        if isinstance(value, bool):
            return spec.default, "expected a number, got a boolean"
        if isinstance(value, (int, float)):
            num = float(value)
        elif isinstance(value, str):
            try:
                num = float(value.strip())
            except ValueError:
                return spec.default, "expected a number"
        else:
            return spec.default, "expected a number"
        if spec.lo is not None and num < spec.lo:
            return spec.default, f"expected a number between {spec.lo:g} and {spec.hi:g}"
        if spec.hi is not None and num > spec.hi:
            return spec.default, f"expected a number between {spec.lo:g} and {spec.hi:g}"
        return num, None

    if kind in ("choice", "browser"):
        if not isinstance(value, str):
            return spec.default, f"expected one of: {', '.join(spec.choices)}"
        low = value.strip().lower()
        if low not in spec.choices:
            return spec.default, f"expected one of: {', '.join(spec.choices)}"
        return low, None

    if kind == "hexcolor":
        if not isinstance(value, str):
            return spec.default, "expected a hex color like #1a2b3c"
        text = value.strip().lstrip("#")
        if len(text) != 6 or any(c not in "0123456789abcdefABCDEF" for c in text):
            return spec.default, "expected a hex color like #1a2b3c"
        return "#" + text.lower(), None

    if kind == "path":
        if isinstance(value, Path):
            return value.expanduser(), None
        if not isinstance(value, str) or not value.strip():
            return spec.default, "expected a file system path"
        return Path(value.strip()).expanduser(), None

    # kind == "str"
    if not isinstance(value, str):
        return spec.default, "expected a string"
    return value, None


# ---------------------------------------------------------------------------
# Loaded configuration
# ---------------------------------------------------------------------------
@dataclass
class Config:
    music_dir: Path
    download_format: str = "mp3"
    download_template: str = DEFAULT_TEMPLATE
    download_concurrency: int = 2
    download_bitrate: str = "192"
    on_exists: str = "ask"
    search_count: int = 20
    ffmpeg_path: Path | None = None
    ambient_source: str = "thumbnail"
    ambient_video_interval: float = 12.0
    visualizer_default: str = "wash"
    motion: str = "full"
    theme: str = "dark"
    high_contrast: bool = False
    accent_override: str | None = None
    restore_session: bool = True
    proxy: str | None = None
    cookies_from_browser: str | None = None
    cookiefile: Path | None = None
    log_level: str = "info"

    # -- derived helpers -------------------------------------------------
    @property
    def cookie_path(self) -> Path | None:
        """Resolve the cookie file (explicit -> auto-detect cookies.txt)."""
        return find_cookiefile(self.cookiefile)

    @property
    def effective_motion(self) -> str:
        """Motion level after environment overrides (NO_COLOR is UI-side)."""
        env = os.environ.get("TIDE_MOTION", "").strip().lower()
        if env in MOTIONS:
            return env
        if os.environ.get("TIDE_REDUCE_MOTION", "").strip() in _TRUE and self.motion == "full":
            return "reduced"
        return self.motion

    def as_table(self) -> dict[str, Any]:
        table: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Path):
                value = str(value)
            table[f.name] = value
        return table


@dataclass
class LoadedConfig:
    config: Config
    issues: list[ConfigIssue]
    created: bool = False
    source: Path | None = None

    @property
    def ok(self) -> bool:
        return not any(i.fatal for i in self.issues)


def _apply_env(config: Config, issues: list[ConfigIssue]) -> None:
    env_map = {
        "TIDE_MUSIC_DIR": ("music_dir", "path"),
        "TIDE_FFMPEG_PATH": ("ffmpeg_path", "path"),
        "TIDE_COOKIEFILE": ("cookiefile", "path"),
        "TIDE_LOG_LEVEL": ("log_level", "choice"),
    }
    for env_name, (key, kind) in env_map.items():
        raw = os.environ.get(env_name)
        if raw is None or not raw.strip():
            continue
        spec = SPECS[key]
        coerced, err = _coerce(
            Spec(kind, spec.default, spec.desc, spec.choices, spec.lo, spec.hi, spec.nullable),
            raw,
        )
        if err:
            issues.append(ConfigIssue(key, f"{env_name} override ignored: {err}"))
            continue
        setattr(config, key, coerced)


def load_config(paths: Paths | None = None, create: bool = True) -> LoadedConfig:
    """Load ``config.toml`` with per-key validation and fallback."""
    paths = paths or get_paths()
    ensure_dirs(paths)
    source = paths.config_file
    issues: list[ConfigIssue] = []
    raw: dict[str, Any] = {}
    created = False

    if source.is_file():
        try:
            with source.open("rb") as fh:
                loaded = tomllib.load(fh)
            if not isinstance(loaded, dict):
                issues.append(ConfigIssue("<file>", "config.toml must be a TOML table; using defaults"))
                raw = {}
            else:
                raw = loaded
        except (tomllib.TOMLDecodeError, OSError) as exc:
            issues.append(ConfigIssue("<file>", f"could not read config.toml ({exc}); using defaults"))
            raw = {}
    elif create:
        raw = {}
        created = True

    values: dict[str, Any] = {}
    for key, spec in SPECS.items():
        if key in raw:
            coerced, err = _coerce(spec, raw[key])
            if err:
                issues.append(ConfigIssue(key, f"invalid value ({err}); using default {spec.default!r}"))
            values[key] = coerced
        else:
            values[key] = spec.default
    for key in raw:
        if key not in SPECS:
            issues.append(ConfigIssue(key, "unknown key (ignored)"))

    config = Config(**values)
    _apply_env(config, issues)

    if created:
        write_default_config(paths)

    return LoadedConfig(config=config, issues=issues, created=created, source=source)


def write_default_config(paths: Paths | None = None) -> Path:
    """Write a documented default config file (never overwrites)."""
    paths = paths or get_paths()
    ensure_dirs(paths)
    target = paths.config_file
    if target.exists():
        return target
    table: dict[str, Any] = {}
    for key, spec in SPECS.items():
        value = spec.default
        if value is None:
            value = ""  # TOML has no null; "" reads back as None for optional keys
        elif isinstance(value, Path):
            value = str(value)
        table[key] = value
    target.write_text(tomli_w.dumps(table), encoding="utf-8")
    return target


def config_documentation() -> list[tuple[str, str, str]]:
    """Return ``(key, default, description)`` rows for docs and ``doctor``."""
    rows = []
    for key, spec in SPECS.items():
        default = spec.default
        if isinstance(default, Path):
            default = str(default)
        rows.append((key, repr(default), spec.desc))
    return rows
