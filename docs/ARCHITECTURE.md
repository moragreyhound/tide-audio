# Tide Architecture Documentation

## Layer Enforcement

1. **media, audio, visual, store must stay importable without a terminal and must never import textual.**
   - All media/audio/visual/store modules are pure Python with no Textual dependencies
   - Tested to import successfully in headless environments

2. **The UI reads the engine only through `snapshot()` and `drain_events()` (plus `position()`); it never touches engine internals.**
   - `TideApp.on_mount` calls `engine.start()`
   - `_tick()` method drains events and updates visualizer
   - Never direct access to engine state attributes

3. **Persistence always goes through `store/schema.py`: `atomic_write_json` (temp file, fsync, rename, keep `.bak`) and `schema_version` with a migration function per version bump.**
   - All state saves go through the store layer
   - Schema migrations handled version-by-version

4. **Anything heavy (numpy, sounddevice, yt_dlp, Pillow) is imported lazily so startup to first frame stays under 400 ms.**
   - Lazy imports in `tide/app.py::on_mount`
   - Lazy imports in `tide/media/ydl.py`
   - Lazy imports in `tide/audio/output.py`

## Layer Strictness

### media/ Layer
- **yt-dlp layer**: search, resolve, download, thumbnails
- **Never imports textual**
- **Never imports audio/visual/store**
- **Exports:** `search()`, `resolve()`, `download()`, `fetch_thumbnail()`
- **Key types:** `Track`, `SearchResult`

### audio/ Layer
- **decoder.py**: FFmpeg-based audio decoding
- **output.py**: sounddevice-based audio output, RingBuffer
- **engine.py**: PlaybackEngine - state machine, queue management
- **analysis.py**: FFT-based audio analysis
- **eq.py**: Equalizer presets and gains
- **Never imports textual**

### visual/ Layer
- **color.py**: RGB/Lab/HSL color space conversions
- **palette.py**: k-means palette extraction from artwork
- **ambient.py**: Eased palette transitions in Lab color space
- **modes/**: Five visualizer modes (spectrum, scope, radial, particles, wash)
- **braille.py**: Braille canvas rendering
- **Never imports textual**

### store/ Layer
- **schema.py**: Atomic JSON writes, schema version migration
- **playlists.py**: Playlist management (create, rename, delete, import, export)
- **history.py**: Search history tracking
- **session.py**: Session state persistence

### ui/ Layer
- **Textual app**: `app.py` - main application shell
- **screens/overlays.py**: HelpScreen, PromptScreen, PickerScreen
- **widgets/**: Header, TrackList, NowPlaying, Visualizer, StatusBar, Rail, DownloadsPanel
- **Never imports from media/, audio/, visual/, store/**

## Event Flow

1. **Key press** → `TideApp.on_key()` → `Keymap.action_for(key, context)` → handler method
2. **Engine events** → `_tick()` → `drain_events()` → event handlers (`_on_state`, `_on_track`, etc.)
3. **Visualizer update** → `viz.feed(frame)` → `viz.render_if_dirty()`
4. **Ambient theme** → `ambient.advance(dt)` → `._apply_theme()` if palette changed
5. **State display** → `._paint_nowplaying(snapshot)` → `NowPlaying.render()`

## State Machine

The playback engine uses a explicit state machine with these states:
- `idle` - nothing playing
- `buffering` - loading/buffering audio
- `playing` - audio actively playing
- `paused` - playback paused
- `ended` - track finished
- `error` - playback failed

State transitions are allowed via: `play()`, `pause()`, `resume()`, `seek()`, `stop()`, `next()`, `prev()`, `toggle_pause()`

## Key Routing Algorithm

1. Normalize the key event
2. Determine the current context:
   - If modal screen is active → context = "overlay"
   - If Input widget has focus → context = "overlay"
   - If TrackList widget has focus → context = "results"
   - If Rail widget has focus → context = "rail"
   - If DownloadsPanel has focus → context = "downloads"
   - Otherwise → context = None
3. Look up action in context-specific binding table
4. If not found in context, fall back to global/transport bindings

## Sound Device Selection

- Pass **int** device indices to sounddevice (a string is matched as a device *name*)
- `locate_ffmpeg()` falls back to the WinGet package path because PATH may be stale in the spawning process
- Default device: "Microsoft Sound Mapper - Output"

## Playback State Diagram

```
      +--------+     play()     +--------+
      | idle   | -----------> | playing|
      +--------+              +--------+
              ^              ^
              |              |
              +-- pause() --+ 
                           v
                      +--------+
                      | paused |
                      +--------+
                           |
                           v
                      +--------+
                      | playing|
                      +--------+
```

## File Structure

```
tide/
  cli.py              argparse entry point (NOT YET WRITTEN - see AGENTS.md)
  config.py           TOML config: per-key validation with fallback, never fatal
  paths.py            platformdirs locations + cookies.txt discovery
  log.py              rotating file logger; logs NEVER go to the terminal
  keys.py             keymap: defaults, keys.toml overrides, context routing
  textutil.py         width-aware truncation, durations, fuzzy match, sanitize_filename
  media/              yt-dlp layer
  audio/              decoder.py (FFmpeg), output.py (sounddevice), engine.py,
                      analysis.py (FFT/beats), eq.py
  visual/             color.py (RGB/Lab/HSL/contrast), palette.py (k-means),
                      braille.py, ambient.py (eased palette transitions),
                      modes/ (spectrum, scope, radial, particles, wash)
  store/              schema.py (atomic JSON + migrations), playlists, history, session
  ui/                 Textual app: app.py, theme.tcss, screens/overlays.py,
                      widgets/ (header, tracklist, nowplaying, visualizer,
                      statusbar, rail, downloadspanel)
tests/                pytest suite (NOT YET WRITTEN)
docs/                 ARCHITECTURE.md, DESIGN.md (NOT YET WRITTEN)
benchmarks/           frame-time and startup measurements (NOT YET WRITTEN)
AGENTS.md             this file
app.py                pre-existing hello-world stub - leave untouched
cookies.txt           real YouTube session cookies - never log its contents
```

## Dependencies

### Direct dependencies:
- `yt-dlp` - YouTube video downloading/processing
- `ffmpeg-python` - FFmpeg Python bindings
- `sounddevice` - Audio output via PortAudio
- `numpy` - Numerical operations, especially for color space conversions
- `Pillow` - Image handling (if cover art features enabled)
- `textual` - TUI framework (UI layer only)
- `tomllib` / `tomli_w` - TOML config file parsing/writing

### Optional/conditional dependencies:
- `ffmpeg` binary - required for audio decoding; located via `locate_ffmpeg()`
- `ffmpeg` via WinGet - automatic fallback when PATH is stale
- `cookies.txt` - YouTube session cookies for authenticated requests

## Deferred Heavy Imports

To keep startup under 400ms, heavy imports are deferred:

| Module | Import location | Reason |
|--------|----------------|--------|
| numpy | `tide/visual/color.py`, `tide/visual/palette.py` | Color space conversions |
| sounddevice | `tide/audio/output.py` | Audio stream setup |
| yt_dlp | `tide/media/ydl.py` | Search/download operations |
| Pillow | Cover art features (if enabled) | Image processing |

All deferred imports are triggered lazily from within handler methods, not at module load time.

## Session Persistence

Session state is persisted using `atomic_write_json` pattern:
1. Write to temp file
2. `fsync` the file descriptor
3. Rename temp file to target (atomic)
4. Keep `.bak` backup file

Schema migrations are versioned with `schema_version` and a migration function per version bump. The current schema version is `X` (to be determined on first release).

## Testing Strategy

- **Smoke tests** (`smoke_*.py`): Full integration tests, 24 checks in UI smoke test
- **Unit tests** (`tests/`): pytest suite, no live network
  - Config validation + fallback
  - Key normalization + context routing
  - Text helpers (widths, durations, sanitize)
  - Palette determinism (fixed seed)
  - Mode determinism
  - Ambient easing
  - Media parsing against fake extractor
- **Benchmark tests** (`tests/test_benchmarks.py`): Frame-time distribution, startup-to-first-frame
- **Never the live network in tests**: All tests use canned payloads or local artifacts