"""Audio output: ring buffer + callback-based PortAudio stream.

* :class:`RingBuffer` holds at least 500 ms of PCM (default: 1 s). Writers
  block with backpressure when full; readers take whatever is available.
* :class:`AudioOutput` runs a sounddevice callback stream. The callback never
  blocks on I/O beyond a non-blocking buffer read: underruns insert silence,
  count themselves, and are reported as debug events - never raised.
* Volume changes apply as a short linear ramp (~5 ms) inside the callback,
  so play/pause and the volume keys cannot click.
* The ring reference can be swapped atomically while the stream runs; this is
  how a gapless track transition hands audio over without stopping output.
* No device at all: ``status == "no_device"`` and the app continues in silent
  mode (browsing/downloading still work).
"""

from __future__ import annotations

import threading
import time
from typing import Callable

from tide.log import get_logger
from tide.audio.decoder import BYTES_PER_FRAME, CHANNELS, SAMPLE_RATE

log = get_logger(__name__)

DEFAULT_CAPACITY_SECONDS = 1.0
RAMP_SECONDS = 0.005
UNDERRUN_LOG_INTERVAL = 2.0


class RingBuffer:
    """Byte ring buffer with blocking producer and non-blocking consumer."""

    def __init__(self, capacity_bytes: int) -> None:
        self.capacity = max(int(capacity_bytes), BYTES_PER_FRAME)
        self._buf = bytearray(self.capacity)
        self._read_pos = 0
        self._write_pos = 0
        self._size = 0
        self._cond = threading.Condition()
        self.total_written = 0
        self.total_read = 0

    @classmethod
    def with_seconds(cls, seconds: float, sample_rate: int = SAMPLE_RATE) -> "RingBuffer":
        return cls(int(seconds * sample_rate) * BYTES_PER_FRAME)

    @property
    def available(self) -> int:
        with self._cond:
            return self._size

    @property
    def free(self) -> int:
        with self._cond:
            return self.capacity - self._size

    @property
    def is_empty(self) -> bool:
        with self._cond:
            return self._size == 0

    @property
    def available_seconds(self) -> float:
        return (self.available // BYTES_PER_FRAME) / SAMPLE_RATE

    def write(self, data: bytes, *, block: bool = True, timeout: float | None = None) -> int:
        """Write as much as fits; blocks (with optional timeout) when full."""
        if not data:
            return 0
        view = memoryview(data)
        written = 0
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._cond:
            while written < len(view):
                while self._size >= self.capacity:
                    if not block:
                        return written
                    if deadline is not None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            return written
                        self._cond.wait(remaining)
                    else:
                        self._cond.wait(0.1)
                chunk = min(len(view) - written, self.capacity - self._size)
                # may wrap
                first = min(chunk, self.capacity - self._write_pos)
                self._buf[self._write_pos : self._write_pos + first] = view[written : written + first]
                rest = chunk - first
                if rest:
                    self._buf[0:rest] = view[written + first : written + chunk]
                self._write_pos = (self._write_pos + chunk) % self.capacity
                self._size += chunk
                written += chunk
                self.total_written += chunk
                self._cond.notify_all()
        return written

    def read(self, max_bytes: int) -> bytes:
        """Take up to ``max_bytes`` (rounded down to whole frames). Never blocks."""
        max_bytes = max(0, max_bytes) // BYTES_PER_FRAME * BYTES_PER_FRAME
        if max_bytes == 0:
            return b""
        with self._cond:
            take = min(max_bytes, self._size)
            if take <= 0:
                return b""
            first = min(take, self.capacity - self._read_pos)
            out = bytes(self._buf[self._read_pos : self._read_pos + first])
            rest = take - first
            if rest:
                out += bytes(self._buf[0:rest])
            self._read_pos = (self._read_pos + take) % self.capacity
            self._size -= take
            self.total_read += take
            self._cond.notify_all()
            return out

    def clear(self) -> None:
        with self._cond:
            self._read_pos = 0
            self._write_pos = 0
            self._size = 0
            self._cond.notify_all()

    def discard_to_frame(self) -> int:
        """Drop trailing bytes so the buffer ends on a frame boundary."""
        with self._cond:
            excess = self._size % BYTES_PER_FRAME
            if excess:
                self._size -= excess
            return excess


class AudioOutput:
    """sounddevice output stream driven by a ring buffer."""

    def __init__(
        self,
        sample_rate: int = SAMPLE_RATE,
        channels: int = CHANNELS,
        blocksize: int = 1024,
        ring: RingBuffer | None = None,
        on_event: Callable[[str, dict], None] | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.blocksize = blocksize
        self._ring: RingBuffer | None = ring
        self._ring_lock = threading.Lock()
        self._on_event = on_event

        self._stream = None
        self._volume = 0.8
        self._target_volume = 0.8
        self._muted = False
        self._ramp_from = 0.8
        self._ramp_left = 0
        self._ramp_step = 0.0

        self.underruns = 0
        self._last_underrun_log = 0.0
        self.device_name: str | None = None
        self.status: str = "idle"  # idle | ok | no_device | failed | fallback
        self._fallback_notified = False
        self._closed = False

    # -- ring management -------------------------------------------------
    @property
    def ring(self) -> RingBuffer | None:
        with self._ring_lock:
            return self._ring

    def set_ring(self, ring: RingBuffer | None) -> None:
        """Atomically swap the ring the callback reads from (gapless hand-off)."""
        with self._ring_lock:
            self._ring = ring

    # -- volume ----------------------------------------------------------
    @property
    def volume(self) -> float:
        return self._volume

    @property
    def muted(self) -> bool:
        return self._muted

    @property
    def effective_volume(self) -> float:
        return 0.0 if self._muted else self._volume

    def set_volume(self, value: float) -> None:
        self._volume = max(0.0, min(1.0, float(value)))
        self._begin_ramp()

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self._begin_ramp()

    def _begin_ramp(self) -> None:
        target = self.effective_volume
        ramp_samples = max(1, int(self.sample_rate * RAMP_SECONDS))
        self._ramp_from = self._target_volume
        self._ramp_step = (target - self._target_volume) / ramp_samples
        self._ramp_left = ramp_samples
        self._target_volume = target

    def fade_to(self, value: float, seconds: float = 0.3) -> None:
        """One-shot ramp to an absolute level (play/pause fades).

        Unlike :meth:`set_volume` this does not change ``self._volume``; the
        next regular volume call ramps from wherever this fade ends.
        """
        target = max(0.0, min(1.0, float(value)))
        ramp_samples = max(1, int(self.sample_rate * max(0.001, seconds)))
        self._ramp_from = self._target_volume
        self._ramp_step = (target - self._target_volume) / ramp_samples
        self._ramp_left = ramp_samples
        self._target_volume = target

    # -- stream lifecycle ------------------------------------------------
    @staticmethod
    def device_report() -> tuple[bool, str]:
        """``(has_output, description)`` without opening a stream."""
        try:
            import sounddevice as sd

            devices = sd.query_devices()
        except Exception as exc:  # pragma: no cover - PortAudio missing
            return False, f"audio unavailable ({exc.__class__.__name__})"
        outputs = [
            f"{i}: {d['name']}"
            for i, d in enumerate(devices)
            if isinstance(d, dict) and d.get("max_output_channels", 0) > 0
        ]
        if not outputs:
            return False, "no output devices"
        return True, outputs[0]

    def start(self) -> bool:
        """Open and start the stream. Returns False in silent mode."""
        if self._closed:
            return False
        if self._stream is not None:
            return True
        try:
            import sounddevice as sd
        except Exception as exc:  # pragma: no cover - PortAudio missing
            self.status = "failed"
            self._emit("device", {"status": self.status, "detail": str(exc)})
            return False

        attempts: list[tuple[int | None, str]] = []
        try:
            current = sd.default.device
            out_index = current[1] if isinstance(current, (list, tuple)) else current
            if out_index in (None, -1):
                attempts.append((None, "default device"))
            else:
                attempts.append((int(out_index), "selected device"))
        except Exception:  # pragma: no cover
            attempts.append((None, "default device"))
        attempts.append((None, "system default"))

        errors: list[str] = []
        for device, label in attempts:
            try:
                stream = sd.OutputStream(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                    dtype="float32",
                    blocksize=self.blocksize,
                    device=device,
                    callback=self._callback,
                )
                stream.start()
            except Exception as exc:
                errors.append(f"{label}: {exc}")
                continue
            self._stream = stream
            is_fallback = bool(errors) and label == "system default"
            self.status = "fallback" if is_fallback else "ok"
            try:
                self.device_name = str(sd.query_devices(stream.device)["name"])
            except Exception:  # pragma: no cover
                self.device_name = label
            if is_fallback and not self._fallback_notified:
                self._fallback_notified = True
                self._emit(
                    "device",
                    {"status": "fallback", "detail": "using the system default device"},
                )
            self._begin_ramp()
            return True

        # Nothing opened: silent mode rather than a crash.
        self.status = "no_device"
        detail = "; ".join(errors[-2:]) if errors else "no output devices"
        self._emit("device", {"status": "no_device", "detail": detail})
        return False

    def stop(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:  # pragma: no cover
                pass

    def close(self) -> None:
        self._closed = True
        self.stop()

    def is_playing(self) -> bool:
        return self._stream is not None

    # -- callback --------------------------------------------------------
    def _callback(self, outdata, frames, time_info, status) -> None:  # noqa: ANN001
        try:
            if status:
                self.underruns += 1
                self._log_underrun(status)
            ring = self._ring
            need = frames * self.channels
            data = ring.read(need * 4) if ring is not None else b""

            import numpy as np

            if data:
                samples = np.frombuffer(data, dtype=np.float32).reshape(-1, self.channels)
            else:
                samples = np.zeros((0, self.channels), dtype=np.float32)

            if samples.shape[0] < frames:
                # Underrun: silence keeps the stream alive.
                pad = frames - samples.shape[0]
                samples = np.concatenate(
                    [samples, np.zeros((pad, self.channels), dtype=np.float32)]
                )
                if ring is not None and ring.available == 0:
                    self.underruns += 1
                    self._log_underrun("buffer empty")

            factors = self._ramp_factors(frames)
            outdata[:] = samples * factors[:, None]
        except Exception as exc:  # pragma: no cover - never kill the stream
            outdata[:] = 0.0
            log.debug("output callback error: %s", exc)

    def _ramp_factors(self, frames: int):
        import numpy as np

        target = self._target_volume
        if self._ramp_left <= 0:
            if np.isclose(self._ramp_from, target):
                self._ramp_from = target
                return np.full(frames, target, dtype=np.float32)
            self._ramp_from = target
            return np.full(frames, target, dtype=np.float32)

        take = min(frames, self._ramp_left)
        ramp = self._ramp_from + self._ramp_step * np.arange(1, take + 1, dtype=np.float64)
        ramp = np.clip(ramp, 0.0, 1.0)
        if take < frames:
            rest = np.full(frames - take, ramp[-1] if take else target, dtype=np.float64)
            factors = np.concatenate([ramp, rest])
        else:
            factors = ramp
        self._ramp_from = float(factors[-1])
        self._ramp_left -= take
        if self._ramp_left <= 0:
            self._ramp_from = target
        return factors.astype(np.float32)

    # -- helpers ---------------------------------------------------------
    def _log_underrun(self, status) -> None:  # noqa: ANN001
        now = time.monotonic()
        if now - self._last_underrun_log >= UNDERRUN_LOG_INTERVAL:
            self._last_underrun_log = now
            log.debug("underrun: %s (total %d)", status, self.underruns)

    def _emit(self, kind: str, data: dict) -> None:
        if self._on_event is not None:
            try:
                self._on_event(kind, data)
            except Exception:  # pragma: no cover - listeners must not break audio
                pass
