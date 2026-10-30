# Tide Design Documentation

## Architecture Decisions

### argparse over typer
**Decision:** Use Python's built-in `argparse` module rather than `typer` or `click`.

**Rationale:**
- No external dependency addition
- `typer` requires `rich` which adds ~3MB to the package
- `argparse` is sufficient for the CLI's relatively simple needs
- Full control over error messages and help text formatting
- Consistent with the project's minimalist philosophy

### Context-scoped keymap groups
**Decision:** Keymaps are organized into contextual groups that determine routing priority.

**Structure:**
- `GLOBAL` - always routable regardless of context
- `TRANSPORT` - always routable alongside global
- `RESULTS` - routable when focus is on list/track views
- `RAIL` - routable when focus is on playlist rail
- `DOWNLOADS` - routable when focus is on downloads panel
- `OVERLAY` - routable when modal screens (help, pickers) are active

**Rationale:**
- Prevents key conflicts between different UI states
- Same key can have different meanings in different contexts (e.g., `p` = previous track in transport, add to playlist in results)
- Natural language mental model: "where am I, what can I do?"
- Enables the `TideApp.on_key` routing logic that checks context first, then falls back to global/transport

**Rationale for NOT using Typer's built-in context:**
Typer's context system is less flexible for this project's needs. The custom keymap system in `tide/keys.py` provides finer-grained control over context switching and conflict resolution.

### RGB as NamedTuple
**Decision:** Use `NamedTuple` for RGB color representation.

**Decision record:**
- Type: `RGB = NamedTuple('RGB', [('r', int), ('g', int), ('b', int)])`
- Used throughout: `tide/visual/color.py`, `tide/visual/palette.py`, `tide/visual/ambient.py`

**Rationale:**
- Immutable color records (frozen by nature of NamedTuple)
- Supports `slots`-like memory efficiency
- Can be used as dict keys (e.g., in palette hashing)
- Supports tuple unpacking: `r, g, b = color`
- Supports `frozenset` usage for set operations
- Compatible with `PIL.ImagePalette` and other RGB representations

**Alternative considered:** Simple `class` or `dataclass` - NamedTuple was chosen for its simplicity and immutability without requiring `from __future__ import annotations` or complex metaclasses.

### Optional TOML keys stored as "" <-> None
**Decision:** Write optional TOML keys as empty string `""`, read back as `None`.

**Decision record:**
- TOML has no null concept; `""` is the closest representation
- `config.py:_coerce()` handles this conversion explicitly
- `write_default_config()` writes `""` for null values
- `load_config()` reads `""` back as `None`
- The `nullable` spec field controls this behavior

**Rationale:**
- Preserves round-trip fidelity: config written by this app can be read by other TOML parsers
- Other TOML parsers may not understand `null` syntax
- Users can write `key = ""` in their config and it will be read as `None`
- Users can write `key = "value"` and it will be read as `"value"`

**Alternative considered:** Using `nil` or `null` in TOML - rejected because many TOML parsers don't support it natively.

### downloads as a dock-right sidebar
**Decision:** The downloads panel is implemented as a dock on the right side of the window.

**Rationale:**
- TCSS (Tide's custom stylesheet language) has no `top/right/bottom/z-index` properties
- Uses `dock: right` layout instead
- Provides consistent behavior across window sizes
- Matches the project's terminal aesthetic

**Alternative considered:** Absolute positioning in TCSS - rejected because TCSS has no absolute positioning support.

### Three tabs (Search/Queue/Library) with playlists living in the rail
**Decision:** The UI has three main views (Search, Queue, Library) and playlists are managed in a sidebar rail.

**Rationale:**
- Search tab: for searching YouTube or local files
- Queue tab: for managing the playback queue
- Library tab: for browsing local audio files
- Playlists appear in the sidebar rail for easy access and management
- Keyboard-driven workflow: `1`/`2`/`3` to switch tabs, `ctrl+b` to toggle rail

**Alternative considered:** Full-screen playlist modal - rejected because it disrupts the continuous UI flow.

### ctrl+r retry
**Decision:** The `ctrl+r` key triggers a retry of the current operation.

**Rationale:**
- Provides a keyboard-driven way to retry failed operations
- Consistent with the project's keyboard-first design
- Handles transient network errors gracefully
- Visually indicated in the status bar

**Implementation:** `tide/keys.py` maps `ctrl+r` to the `retry` action, `tide/app.py` `_act_retry()` checks `can_retry` and either retries or shows a message.

### Cover art embedded via yt-dlp's EmbedThumbnail/FFmpegMetadata with square cropping attempted through postprocessor_args
**Decision:** Use yt-dlp's `EmbedThumbnail` and `FFmpegMetadata` postprocessors, with square crop attempted through `postprocessor_args`.

**Rationale:**
- yt-dlp's built-in postprocessors are the most reliable way to embed cover art
- `EmbedThumbnail` embeds the thumbnail from the video info
- `FFmpegMetadata` adds metadata including album art
- `postprocessor_args` with `crop=640:640:0:0` attempts square cropping
- Square crop is attempted but may not always succeed depending on source aspect ratio

**Alternative considered:** downscale/crop in Python after download - rejected because it adds post-processing step and may degrade image quality.

## Project Structure

```
tide/                 the package (strict layering)
  cli.py              argparse entry point: play/search/download/playlist/doctor
  config.py           TOML config: per-key validation with fallback, never fatal
  paths.py            platformdirs locations + cookies.txt discovery
  log.py              rotating file logger; logs NEVER go to the terminal
  keys.py             keymap: defaults, keys.toml overrides, context routing
  textutil.py         width-aware truncation, durations, fuzzy match, sanitize_filename
  media/              yt-dlp layer: ydl.py (shared factory + retries),
                      search.py, resolve.py, download.py, thumbnails.py, errors.py
  audio/              decoder.py (FFmpeg), output.py (sounddevice), engine.py,
                      analysis.py (FFT/beats), eq.py
  visual/             color.py (RGB/Lab/HSL/contrast), palette.py (k-means),
                      braille.py, ambient.py (eased palette transitions),
                      modes/ (spectrum, scope, radial, particles, wash)
  store/              schema.py (atomic JSON + migrations), playlists, history, session
  ui/                 Textual app: app.py, theme.tcss, screens/overlays.py,
                      widgets/ (header, tracklist, nowplaying, visualizer,
                      statusbar, rail, downloadspanel)
tests/                pytest suite
docs/                 ARCHITECTURE.md, DESIGN.md
benchmarks/           frame-time and startup measurements
cookies.txt           real YouTube session cookies - never log its contents
app.py                pre-existing hello-world stub - leave untouched
```

## README (planned)

See `docs/ARCHITECTURE.md` for detailed architectural overview.

## CHANGELOG (planned)

Change log following Keep a Changelog format, documenting:
- New features
- Bug fixes  
- Deprecations
- Breaking changes

## ARCHITECTURE.md (planned)

Detailed architecture overview covering:
- Layer strictness (media/audio/visual/store never import textual)
- Event flow through the system
- State management patterns
- Key routing algorithm