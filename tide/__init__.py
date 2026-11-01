"""Tide - a terminal music player built on yt-dlp, FFmpeg, and Textual.

The package is split into strict layers:

* ``media``   - search, resolution, download (yt-dlp)
* ``audio``   - decoder, output, engine, analysis (FFmpeg, sounddevice, NumPy)
* ``visual``  - palette extraction and visualizer modes (Pillow, NumPy)
* ``store``   - playlists, history, session (JSON + TOML persistence)
* ``ui``      - the Textual application (imported lazily by the CLI)

Nothing outside ``ui`` imports Textual, so the non-UI layers stay testable
without a terminal. 
totally fucking vibecoded wtf is even this
"""

__version__ = "0.1.0"
__app_name__ = "tide"

# Deferred heavy imports -------------------------------------------------
def heavy_import_report() -> dict[str, str]:
    """Return import status of optional heavy dependencies.

    Used by ``tide doctor`` and the startup profiler. Importing numpy,
    sounddevice and yt_dlp is deferred until they are actually needed so the
    first frame appears quickly.
    """
    import importlib

    report: dict[str, str] = {}
    for name in ("numpy", "PIL", "sounddevice", "yt_dlp", "ffmpeg", "textual"):
        try:
            mod = importlib.import_module(name)
            report[name] = getattr(mod, "__version__", "ok")
        except Exception as exc:  # pragma: no cover - depends on host
            report[name] = f"missing ({exc.__class__.__name__})"
    return report
