"""FFmpeg decoder: spawns a subprocess piping raw float32 stereo PCM.

Uses ``ffmpeg-python`` to build the command graph (input seek, ``atempo``
speed, chained ``equalizer`` peaking bands) and to run it asynchronously.

    ffmpeg -nostdin -ss 12.5 -vn -i <source> -filter_complex ... \
        -f f32le -ac 2 -ar 48000 pipe:1

Seeking restarts the process with ``-ss`` (never rewinds the pipe). The
stderr stream is drained by a daemon thread so FFmpeg can never block on a
full pipe; the tail is kept for error reporting.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Any, Sequence

import ffmpeg as ff

from tide.log import get_logger

log = get_logger(__name__)

SAMPLE_RATE = 48000
CHANNELS = 2
BYTES_PER_FRAME = 4 * CHANNELS  # f32le stereo

_STDERR_LINES = 40


def locate_ffmpeg(configured: str | Path | None = None) -> str | None:
    """Find the ffmpeg binary: config override -> PATH -> known installers."""
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
        if candidate.is_dir():
            for name in ("ffmpeg.exe", "ffmpeg"):
                inner = candidate / name
                if inner.is_file():
                    return str(inner)
    found = shutil.which("ffmpeg")
    if found:
        return found
    # PATH may be stale in this process (e.g. freshly installed via winget).
    if sys.platform == "win32":
        packages = Path.home() / "AppData/Local/Microsoft/WinGet/Packages"
        try:
            for match in sorted(packages.glob("Gyan.FFmpeg*/ffmpeg-*/bin/ffmpeg.exe")):
                return str(match)
        except OSError:  # pragma: no cover
            pass
    else:
        for path in ("/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"):
            if Path(path).is_file():
                return path
    return None


def _windows_hidden() -> dict[str, Any]:
    """Prevent a console window flashing for each decoder spawn."""
    if sys.platform != "win32":  # pragma: no cover - POSIX
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = subprocess.SW_HIDE
    return {"startupinfo": startupinfo, "creationflags": subprocess.CREATE_NO_WINDOW}


def build_graph(
    source: str,
    *,
    start_at: float = 0.0,
    speed: float = 1.0,
    eq_gains: Sequence[float] | None = None,
    http_headers: dict[str, str] | None = None,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    video_disabled: bool = True,
) -> Any:
    """Build the ffmpeg-python output node for a decode.

    Exposed separately so tests can inspect the compiled command without
    spawning a process.
    """
    options: dict[str, Any] = {"nostdin": None}
    if start_at > 0:
        options["ss"] = f"{start_at:.3f}"
    if http_headers:
        options["headers"] = "".join(f"{k}: {v}\r\n" for k, v in http_headers.items())
    stream = ff.input(source, **options)

    audio = stream
    if speed and abs(speed - 1.0) > 1e-6:
        audio = audio.filter("atempo", round(float(speed), 4))
    for band in _eq_filter_specs(eq_gains):
        audio = audio.filter(
            "equalizer",
            f=band["f"],
            t="peaking",
            width_type="o",
            width=band["width"],
            g=band["g"],
        )

    output_kwargs: dict[str, Any] = {
        "format": "f32le",
        "ac": channels,
        "ar": sample_rate,
    }
    if video_disabled:
        output_kwargs["vn"] = None
    return audio.output("pipe:1", **output_kwargs).global_args(
        "-loglevel", "error", "-nostats"
    )


def _eq_filter_specs(eq_gains: Sequence[float] | None) -> list[dict[str, Any]]:
    """Peaking-filter specs for the 10-band EQ (bands with |g| < 0.5 skipped)."""
    if not eq_gains:
        return []
    from tide.audio.eq import BAND_FREQUENCIES

    specs: list[dict[str, Any]] = []
    for freq, gain in zip(BAND_FREQUENCIES, eq_gains):
        if abs(gain) >= 0.5:
            specs.append({"f": freq, "width": 1.0 if freq < 250 else 0.8, "g": round(float(gain), 2)})
    return specs


class FFmpegDecoder:
    """One ffmpeg process producing float32le PCM on stdout."""

    def __init__(self, ffmpeg_path: str | Path | None = None) -> None:
        self.ffmpeg_path = locate_ffmpeg(ffmpeg_path)
        self._process: subprocess.Popen[bytes] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_LINES)
        self._lock = threading.Lock()
        self.start_at = 0.0
        self.speed = 1.0
        self.bytes_read = 0

    # -- lifecycle -------------------------------------------------------
    @property
    def available(self) -> bool:
        return self.ffmpeg_path is not None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def finished(self) -> bool:
        """True once the process exited (EOF or crash)."""
        return self._process is not None and self._process.poll() is not None

    @property
    def returncode(self) -> int | None:
        return self._process.poll() if self._process else None

    def start(
        self,
        source: str,
        *,
        start_at: float = 0.0,
        speed: float = 1.0,
        eq_gains: Sequence[float] | None = None,
        http_headers: dict[str, str] | None = None,
    ) -> None:
        if not self.ffmpeg_path:
            raise RuntimeError("ffmpeg binary not found (set ffmpeg_path in config)")
        self.stop()
        graph = build_graph(
            source,
            start_at=start_at,
            speed=speed,
            eq_gains=eq_gains,
            http_headers=http_headers,
        )
        argv = graph.compile(cmd=self.ffmpeg_path)
        log.debug("decoder spawn: %s", " ".join(argv[:8]) + " ...")
        self._stderr_tail.clear()
        self._process = subprocess.Popen(  # noqa: S603 - argv from ffmpeg-python
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=10**8,
            **_windows_hidden(),
        )
        self.start_at = float(start_at)
        self.speed = float(speed)
        self.bytes_read = 0
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def stop(self, timeout: float = 1.0) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=timeout)
                except subprocess.TimeoutExpired:  # pragma: no cover - stubborn child
                    process.kill()
                    process.wait(timeout=timeout)
        except OSError as exc:  # pragma: no cover
            log.debug("decoder stop failed: %s", exc)
        for stream in (process.stdout, process.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:  # pragma: no cover
                pass

    # -- reading ---------------------------------------------------------
    def read(self, size: int) -> bytes:
        """Read up to ``size`` bytes; ``b""`` means EOF or not running."""
        process = self._process
        if process is None or process.stdout is None:
            return b""
        try:
            reader = getattr(process.stdout, "read1", None)
            data = reader(size) if reader is not None else process.stdout.read(size)
        except (OSError, ValueError):
            return b""
        if data:
            self.bytes_read += len(data)
        return data or b""

    @property
    def samples_read(self) -> int:
        return self.bytes_read // BYTES_PER_FRAME

    @property
    def output_seconds(self) -> float:
        return self.samples_read / SAMPLE_RATE

    @property
    def source_seconds(self) -> float:
        """Position in source time (atempo scales output to source)."""
        return self.start_at + self.output_seconds * self.speed

    # -- diagnostics -----------------------------------------------------
    def _drain_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        try:
            for raw in iter(process.stderr.readline, b""):
                line = raw.decode("utf-8", errors="replace").strip()
                if line:
                    self._stderr_tail.append(line)
        except (OSError, ValueError):  # pragma: no cover - closed pipe
            pass

    def stderr_tail(self, lines: int = 3) -> str:
        if not self._stderr_tail:
            return ""
        tail = list(self._stderr_tail)[-lines:]
        return " | ".join(tail)
