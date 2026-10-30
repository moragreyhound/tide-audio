"""Playback engine: explicit state machine, own thread, event queue.

Threading model (see docs/ARCHITECTURE.md):

    UI thread ---- commands (thread-safe methods) ----> engine loop thread
       ^                                                   |
       |            deque[EngineEvent] (drop-oldest)       |
       +----------- drain_events() <-----------------------+
                                                      reader thread(s)
                                                      (decoder -> ring,
                                                       analyzer frames)

* The engine loop thread owns state transitions (:class:`StateMachine` with
  an explicit allow-list), EOF handling, prefetch scheduling and position
  ticks.
* One reader thread per decode context fills a :class:`RingBuffer` with
  backpressure and feeds the :class:`Analyzer` for the active context.
* The sounddevice callback thread only reads the ring and applies the volume
  ramp; it never blocks on I/O.
* Gapless hand-off: when the active decoder reports EOF, a worker resolves
  and spawns the next decoder into a staging ring. The output ring is swapped
  atomically once the current ring has drained.
* The UI never reads engine internals - only ``snapshot()`` and
  ``drain_events()``.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable

from tide.audio.analysis import Analyzer, AnalysisFrame
from tide.audio.decoder import BYTES_PER_FRAME, CHANNELS, FFmpegDecoder, SAMPLE_RATE
from tide.audio.output import AudioOutput, RingBuffer
from tide.log import get_logger
from tide.store.playlists import Track

log = get_logger(__name__)

RING_SECONDS = 1.0            # >= 500 ms required by spec
PREROLL_SECONDS = 0.10        # buffered before output starts
STALL_SECONDS = 0.04          # below this while playing -> buffering
RECOVER_SECONDS = 0.10
READ_CHUNK = 16384            # 2048 frames: exactly one analyzer window
TICK_SECONDS = 0.02           # engine loop cadence (50 Hz)
POSITION_EVENT_SECONDS = 0.25
RESUME_RESTART_THRESHOLD = 3.0  # prev restarts the track past 3 seconds
PREFETCH_EARLY_SECONDS = 8.0  # resolve next stream URL this long before EOF
MAX_EVENTS = 512


class EngineState(str, Enum):
    IDLE = "idle"
    RESOLVING = "resolving"
    BUFFERING = "buffering"
    PLAYING = "playing"
    PAUSED = "paused"
    SEEKING = "seeking"
    ENDED = "ended"
    ERROR = "error"


class IllegalTransition(Exception):
    """Raised by :meth:`StateMachine.to` when a transition is not allowed."""


#: The complete, tested transition table.
ALLOWED_TRANSITIONS: dict[EngineState, frozenset[EngineState]] = {
    EngineState.IDLE: frozenset({EngineState.RESOLVING}),
    EngineState.RESOLVING: frozenset(
        {
            EngineState.BUFFERING,
            EngineState.PLAYING,
            EngineState.PAUSED,
            EngineState.ERROR,
            EngineState.IDLE,
            EngineState.ENDED,
        }
    ),
    EngineState.BUFFERING: frozenset(
        {
            EngineState.PLAYING,
            EngineState.PAUSED,
            EngineState.SEEKING,
            EngineState.ERROR,
            EngineState.IDLE,
            EngineState.ENDED,
        }
    ),
    EngineState.PLAYING: frozenset(
        {
            EngineState.PAUSED,
            EngineState.BUFFERING,
            EngineState.SEEKING,
            EngineState.ENDED,
            EngineState.ERROR,
            EngineState.IDLE,
            EngineState.RESOLVING,
        }
    ),
    EngineState.PAUSED: frozenset(
        {
            EngineState.PLAYING,
            EngineState.BUFFERING,
            EngineState.SEEKING,
            EngineState.IDLE,
            EngineState.RESOLVING,
            EngineState.ERROR,
            EngineState.ENDED,
        }
    ),
    EngineState.SEEKING: frozenset(
        {
            EngineState.BUFFERING,
            EngineState.PLAYING,
            EngineState.PAUSED,
            EngineState.RESOLVING,
            EngineState.ERROR,
            EngineState.IDLE,
        }
    ),
    EngineState.ENDED: frozenset(
        {EngineState.RESOLVING, EngineState.BUFFERING, EngineState.IDLE, EngineState.PLAYING}
    ),
    EngineState.ERROR: frozenset({EngineState.RESOLVING, EngineState.IDLE}),
}


class StateMachine:
    """Explicit engine state machine (never raises inside the engine loop)."""

    def __init__(self, initial: EngineState = EngineState.IDLE) -> None:
        self.state = initial
        self.history: list[tuple[float, EngineState]] = [(time.monotonic(), initial)]

    def can(self, new: EngineState) -> bool:
        if new == self.state:
            return True
        return new in ALLOWED_TRANSITIONS[self.state]

    def to(self, new: EngineState) -> EngineState:
        """Perform a transition or raise :class:`IllegalTransition`."""
        if new == self.state:
            return self.state
        if new not in ALLOWED_TRANSITIONS[self.state]:
            raise IllegalTransition(f"{self.state.value} -> {new.value} is not an allowed transition")
        self.state = new
        self.history.append((time.monotonic(), new))
        if len(self.history) > 64:
            del self.history[:-64]
        return new

    def try_to(self, new: EngineState) -> bool:
        """Log-and-continue variant used by the engine loop."""
        if new == self.state:
            return True
        if not self.can(new):
            log.debug("ignored transition %s -> %s", self.state.value, new.value)
            return False
        self.to(new)
        return True


@dataclass(frozen=True)
class EngineEvent:
    type: str            # state | track | position | frame | error | info | device | debug
    data: dict[str, Any] = field(default_factory=dict)
    at: float = field(default_factory=time.monotonic)


@dataclass(frozen=True)
class EngineSnapshot:
    state: EngineState
    track: Track | None
    position: float
    duration: int | None
    volume: float
    muted: bool
    shuffle: bool
    repeat: str
    speed: float
    buffering: bool
    queue: tuple[Track, ...]
    index: int
    eq_preset: str
    device_status: str
    error: str | None
    can_retry: bool
    prefetching: bool

    @property
    def is_active(self) -> bool:
        return self.state in (
            EngineState.PLAYING,
            EngineState.BUFFERING,
            EngineState.PAUSED,
            EngineState.SEEKING,
        )


@dataclass
class _Context:
    """One decode generation: decoder + its ring + reader thread."""

    track: Track
    decoder: FFmpegDecoder
    ring: RingBuffer
    start_at: float
    speed: float
    generation: int
    source_url: str
    eof: bool = False
    abandoned: bool = False
    reader: threading.Thread | None = None
    error: str | None = None

    @property
    def position(self) -> float:
        played_frames = max(0, (self.decoder.samples_read * BYTES_PER_FRAME - self.ring.available) // BYTES_PER_FRAME)
        return self.start_at + (played_frames / SAMPLE_RATE) * self.speed


def _is_local_source(source: str) -> bool:
    if "://" in source:
        return source.startswith("file://")
    return True


class PlaybackEngine:
    """Owns decoding, output, the queue and the state machine."""

    def __init__(
        self,
        config: Any = None,
        resolver: Callable[[str], Any] | None = None,
        *,
        output: AudioOutput | None = None,
        random_source: random.Random | None = None,
    ) -> None:
        self.config = config
        self._resolver = resolver
        self._rng = random_source or random.Random()

        self._lock = threading.RLock()
        self._sm = StateMachine()
        self._events: deque[EngineEvent] = deque(maxlen=MAX_EVENTS)
        self._commands: deque[tuple[str, dict[str, Any]]] = deque()
        self._cond = threading.Condition(self._lock)

        self._queue: list[Track] = []
        self._index = 0
        self._shuffle = False
        self._repeat = "off"        # off | queue | one
        self._speed = 1.0
        self._eq_gains: list[float] = [0.0] * 10
        self._eq_preset = "flat"
        self._generation = 0

        self._ctx: _Context | None = None
        self._staging: _Context | None = None
        self._staging_pending = False
        self._prefetched: tuple[str, Any] | None = None  # (source, resolver result)
        self._prefetching = False
        self._pause_pending = False
        self._paused = False
        self._error: str | None = None
        self._can_retry = False
        self._error_position: float | None = None
        self._last_position_emit = 0.0
        self._stopping = False

        self._analyzer = Analyzer(SAMPLE_RATE)
        self._output = output or AudioOutput(
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            ring=None,
            on_event=self._output_event,
        )
        self._loop: threading.Thread | None = None

    # ==================================================================
    # Lifecycle
    # ==================================================================
    def start(self) -> None:
        """Start the engine loop thread (idempotent)."""
        with self._lock:
            if self._loop is not None:
                return
            self._stopping = False
            self._loop = threading.Thread(target=self._run, name="tide-engine", daemon=True)
            self._loop.start()

    def close(self, *, save_state: bool = True) -> dict[str, Any] | None:
        """Stop everything and return the state to persist (or None)."""
        with self._lock:
            if self._stopping:
                return None
            self._stopping = True
            session_state = self._build_session_state() if save_state else None
            self._generation += 1
            ctx, staging = self._ctx, self._staging
            self._ctx = self._staging = None
            self._cond.notify_all()
        loop = self._loop
        self._loop = None
        if loop is not None and loop.is_alive() and loop is not threading.current_thread():
            loop.join(timeout=1.5)
        for context in (ctx, staging):
            if context is not None:
                context.abandoned = True
                if context.reader is not None:
                    context.reader.join(timeout=1.0)
                context.decoder.stop()
        self._output.close()
        return session_state

    @property
    def stopped(self) -> bool:
        return self._stopping

    # ==================================================================
    # Commands (called from any thread; heavy work is queued)
    # ==================================================================
    def set_queue(self, tracks: Iterable[Track], index: int = 0) -> None:
        with self._lock:
            self._queue = list(tracks)
            self._index = max(0, min(index, max(0, len(self._queue) - 1)))

    def queue_insert(self, tracks: Iterable[Track], index: int | None = None) -> None:
        with self._lock:
            if index is None:
                self._queue.extend(tracks)
            else:
                position = max(0, min(index, len(self._queue)))
                for offset, track in enumerate(tracks):
                    self._queue.insert(position + offset, track)

    def queue_remove(self, index: int) -> Track | None:
        with self._lock:
            if not (0 <= index < len(self._queue)):
                return None
            removed = self._queue.pop(index)
            if index < self._index:
                self._index -= 1
            elif index == self._index:
                self._index = max(0, min(self._index, len(self._queue) - 1))
            return removed

    def queue_move(self, index: int, delta: int) -> bool:
        with self._lock:
            target = index + delta
            if not (0 <= index < len(self._queue)) or not (0 <= target < len(self._queue)):
                return False
            self._queue[index], self._queue[target] = self._queue[target], self._queue[index]
            if self._index == index:
                self._index = target
            elif self._index == target:
                self._index = index
            return True

    def clear_queue(self) -> None:
        with self._lock:
            self._queue.clear()
            self._index = 0

    @property
    def queue(self) -> tuple[Track, ...]:
        with self._lock:
            return tuple(self._queue)

    @property
    def index(self) -> int:
        with self._lock:
            return self._index

    def play(self, index: int, start_at: float = 0.0) -> None:
        with self._lock:
            if not self._queue:
                return
            self._index = max(0, min(index, len(self._queue) - 1))
            self._enqueue("play", {"start_at": start_at})
    def play_track(self, track: Track, queue: list[Track] | None = None, index: int | None = None) -> None:
        with self._lock:
            if queue is not None:
                self._queue = list(queue)
                self._index = index if index is not None else 0
            else:
                self._queue.append(track)
                self._index = len(self._queue) - 1
            self._enqueue("play", {"start_at": 0.0})

    def pause(self) -> None:
        with self._lock:
            if self._sm.state == EngineState.PLAYING:
                self._paused = True
                self._sm.try_to(EngineState.PAUSED)
                self._emit_state()
                threading.Thread(target=self._pause_fade, daemon=True).start()
            elif self._sm.state in (EngineState.BUFFERING, EngineState.RESOLVING, EngineState.SEEKING):
                self._pause_pending = True

    def resume(self) -> None:
        with self._lock:
            if self._sm.state != EngineState.PAUSED:
                return
            self._paused = False
            self._pause_pending = False
            started = self._output.start() if self._output.status != "no_device" else False
            self._output.fade_to(self._output.effective_volume, 0.3)
            silent = self._output.status == "no_device"
            target = EngineState.PLAYING if started or silent else EngineState.BUFFERING
            self._sm.try_to(target)
            self._emit_state()

    def toggle_pause(self) -> None:
        with self._lock:
            state = self._sm.state
        if state == EngineState.PLAYING:
            self.pause()
        elif state in (EngineState.PAUSED,):
            self.resume()
        elif state in (EngineState.IDLE, EngineState.ENDED, EngineState.ERROR):
            with self._lock:
                if self._queue:
                    self.play(self._index)
        else:
            self.pause()  # buffering: arm pause-on-ready

    def seek(self, seconds: float) -> None:
        with self._lock:
            if self._sm.state in (EngineState.IDLE, EngineState.ERROR):
                return
            self._enqueue("seek", {"seconds": max(0.0, float(seconds))})

    def seek_relative(self, delta: float) -> None:
        with self._lock:
            position = self.position()
        self.seek(position + delta)

    def seek_percent(self, fraction: float) -> None:
        with self._lock:
            duration = self.duration()
        if duration:
            self.seek(duration * max(0.0, min(1.0, fraction)))

    def next(self) -> None:
        with self._lock:
            self._enqueue("advance", {"force": True})

    def prev(self) -> None:
        with self._lock:
            self._enqueue("back", {})

    def retry(self) -> None:
        with self._lock:
            if self._sm.state == EngineState.ERROR:
                self._enqueue("play", {"start_at": self._error_position or 0.0})

    def stop(self) -> None:
        with self._lock:
            self._enqueue("stop", {})

    # -- settings (immediate, no queue) ----------------------------------
    def set_volume(self, value: float) -> None:
        self._output.set_volume(value)

    def adjust_volume(self, delta: float) -> float:
        with self._lock:
            new = max(0.0, min(1.0, self._output.volume + delta))
        self._output.set_volume(new)
        return new

    @property
    def volume(self) -> float:
        return self._output.volume

    @property
    def muted(self) -> bool:
        return self._output.muted

    def toggle_mute(self) -> bool:
        self._output.set_muted(not self._output.muted)
        return self._output.muted

    def set_shuffle(self, enabled: bool) -> bool:
        with self._lock:
            self._shuffle = bool(enabled)
            return self._shuffle

    def toggle_shuffle(self) -> bool:
        with self._lock:
            self._shuffle = not self._shuffle
            return self._shuffle

    def cycle_repeat(self) -> str:
        with self._lock:
            order = ("off", "queue", "one")
            self._repeat = order[(order.index(self._repeat) + 1) % len(order)]
            return self._repeat

    @property
    def shuffle(self) -> bool:
        return self._shuffle

    @property
    def repeat(self) -> str:
        return self._repeat

    @property
    def eq_gains(self) -> list[float]:
        """Current per-band gains (session persistence needs them)."""
        with self._lock:
            return list(self._eq_gains)

    def set_speed(self, value: float) -> float:
        value = max(0.5, min(2.0, float(value)))
        with self._lock:
            self._speed = value
            if self._sm.state in (EngineState.PLAYING, EngineState.PAUSED, EngineState.BUFFERING):
                position = self.position()
                self._enqueue("seek", {"seconds": position})
        return value

    def cycle_speed(self, direction: int) -> float:
        steps = (0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0)
        current = self._speed
        if direction >= 0:
            nxt = next((s for s in steps if s > current + 1e-6), steps[-1])
        else:
            nxt = next((s for s in reversed(steps) if s < current - 1e-6), steps[0])
        return self.set_speed(nxt)

    @property
    def speed(self) -> float:
        return self._speed

    def set_eq(self, gains: list[float], preset: str = "custom") -> None:
        from tide.audio.eq import validate_gains

        with self._lock:
            self._eq_gains = validate_gains(gains)
            self._eq_preset = preset
            if self._sm.state in (EngineState.PLAYING, EngineState.PAUSED, EngineState.BUFFERING):
                position = self.position()
                self._enqueue("seek", {"seconds": position})

    @property
    def eq_gains(self) -> list[float]:
        return list(self._eq_gains)

    @property
    def eq_preset(self) -> str:
        return self._eq_preset

    def set_analysis(self, frame: AnalysisFrame | None) -> None:  # pragma: no cover - hook
        return None

    # ==================================================================
    # Observation (UI thread)
    # ==================================================================
    def position(self) -> float:
        with self._lock:
            return self._position_locked()

    def duration(self) -> int | None:
        with self._lock:
            if self._ctx is not None and self._ctx.track.duration:
                return int(self._ctx.track.duration)
            if 0 <= self._index < len(self._queue):
                return self._queue[self._index].duration
            return None

    def _position_locked(self) -> float:
        ctx = self._ctx
        if ctx is None:
            return 0.0
        pos = ctx.position
        duration = ctx.track.duration
        if duration:
            pos = min(pos, float(duration))
        return max(0.0, pos)

    def snapshot(self) -> EngineSnapshot:
        with self._lock:
            track = self._ctx.track if self._ctx else None
            if track is None and 0 <= self._index < len(self._queue):
                track = self._queue[self._index] if self._sm.state != EngineState.IDLE else None
            position = self._position_locked()
            duration = None
            if self._ctx is not None and self._ctx.track.duration:
                duration = int(self._ctx.track.duration)
            elif track is not None:
                duration = track.duration
            state = self._sm.state
            buffering = state == EngineState.BUFFERING
            if state == EngineState.PLAYING and self._ctx is not None:
                available = self._ctx.ring.available_seconds
                buffering = not self._ctx.eof and available < STALL_SECONDS
            return EngineSnapshot(
                state=state,
                track=track,
                position=position,
                duration=duration,
                volume=self._output.volume,
                muted=self._output.muted,
                shuffle=self._shuffle,
                repeat=self._repeat,
                speed=self._speed,
                buffering=buffering,
                queue=tuple(self._queue),
                index=self._index,
                eq_preset=self._eq_preset,
                device_status=self._output.status,
                error=self._error,
                can_retry=self._can_retry,
                prefetching=self._prefetching,
            )

    @property
    def state(self) -> EngineState:
        with self._lock:
            return self._sm.state

    @property
    def latest_frame(self) -> AnalysisFrame | None:
        with self._lock:
            events = [e for e in self._events if e.type == "frame"]
            return events[-1].data.get("frame") if events else None

    def drain_events(self, limit: int = 128) -> list[EngineEvent]:
        out: list[EngineEvent] = []
        with self._lock:
            while self._events and len(out) < limit:
                out.append(self._events.popleft())
        return out

    # ==================================================================
    # Engine loop thread
    # ==================================================================
    def _enqueue(self, name: str, kwargs: dict[str, Any]) -> None:
        with self._lock:
            self._commands.append((name, kwargs))
            self._cond.notify_all()

    def _run(self) -> None:
        log.debug("engine loop started")
        while not self._stopping:
            commands: list[tuple[str, dict[str, Any]]] = []
            with self._cond:
                if not self._commands:
                    self._cond.wait(TICK_SECONDS)
                while self._commands:
                    commands.append(self._commands.popleft())
            for name, kwargs in commands:
                try:
                    self._execute(name, kwargs)
                except Exception as exc:  # pragma: no cover - defensive
                    log.exception("engine command %s failed", name)
                    self._fail(f"{name} failed: {exc}")
            if self._stopping:
                break
            try:
                self._tick()
            except Exception:  # pragma: no cover - loop must survive
                log.exception("engine tick failed")
        log.debug("engine loop stopped")

    def _execute(self, name: str, kwargs: dict[str, Any]) -> None:
        if name == "play":
            self._do_play(kwargs.get("start_at", 0.0))
        elif name == "seek":
            self._do_seek(kwargs.get("seconds", 0.0))
        elif name == "advance":
            self._do_advance(kwargs.get("force", True))
        elif name == "back":
            self._do_back()
        elif name == "stop":
            self._do_stop()

    # -- commands --------------------------------------------------------
    def _do_play(self, start_at: float) -> None:
        with self._lock:
            if not self._queue:
                return
            track = self._queue[self._index]
            generation = self._generation = self._generation + 1
            self._abandon_locked()
            self._staging_pending = False
            self._error = None
            self._can_retry = False
            self._prefetched = None
            self._staging = None
            self._paused = False
            self._sm.try_to(EngineState.RESOLVING)
            self._emit(EngineEvent("state", {"state": EngineState.RESOLVING.value}))
            self._emit(
                EngineEvent(
                    "track",
                    {"track": track, "index": self._index, "queue": tuple(self._queue)},
                )
            )
        threading.Thread(
            target=self._activate_worker,
            args=(track, start_at, generation),
            name=f"tide-resolve-{generation}",
            daemon=True,
        ).start()

    def _do_seek(self, seconds: float) -> None:
        with self._lock:
            if self._sm.state in (EngineState.IDLE, EngineState.ERROR, EngineState.ENDED):
                if self._sm.state == EngineState.ENDED and self._queue:
                    # seeking while ended replays the track
                    self._sm.try_to(EngineState.BUFFERING)
                else:
                    return
            if self._ctx is None:
                if not self._queue:
                    return
                self._do_play(seconds)
                return
            track = self._ctx.track
            was_paused = self._paused
            generation = self._generation = self._generation + 1
            self._abandon_locked()
            self._sm.try_to(EngineState.SEEKING)
            self._emit(EngineEvent("state", {"state": EngineState.SEEKING.value, "position": seconds}))
        threading.Thread(
            target=self._activate_worker,
            args=(track, max(0.0, seconds), generation, was_paused),
            name=f"tide-seek-{generation}",
            daemon=True,
        ).start()

    def _do_advance(self, force: bool) -> None:
        with self._lock:
            index = self._next_index_locked(force=force)
            if index is None:
                self._end_locked()
                return
            self._index = index
        self._do_play(0.0)

    def _do_back(self) -> None:
        with self._lock:
            position = self._position_locked()
            if position > RESUME_RESTART_THRESHOLD:
                restart_at = 0.0
            else:
                restart_at = 0.0
                if self._index > 0:
                    self._index -= 1
                elif self._repeat == "queue" and len(self._queue) > 1:
                    self._index = len(self._queue) - 1
        self._do_play(restart_at)

    def _do_stop(self) -> None:
        with self._lock:
            self._generation += 1
            self._abandon_locked()
            self._sm.try_to(EngineState.IDLE)
            self._output.stop()
            self._emit(EngineEvent("state", {"state": EngineState.IDLE.value}))

    def _next_index_locked(self, *, force: bool) -> int | None:
        size = len(self._queue)
        if size == 0:
            return None
        if self._repeat == "one" and not force:
            return self._index
        if self._shuffle and size > 1:
            choices = [i for i in range(size) if i != self._index]
            return self._rng.choice(choices)
        nxt = self._index + 1
        if nxt < size:
            return nxt
        return nxt if self._repeat == "queue" else None

    # -- workers ---------------------------------------------------------
    def _activate_worker(
        self, track: Track, start_at: float, generation: int, paused: bool | None = None
    ) -> None:
        """Resolve + spawn decoder + preroll, then flip the context live."""
        try:
            resolved = self._resolve(track)
        except Exception as exc:
            self._resolve_failed(track, exc, generation)
            return
        if not self._is_current(generation):
            return

        decoder = FFmpegDecoder(self._ffmpeg_path())
        if not decoder.available:
            self._resolve_failed(track, RuntimeError("ffmpeg not found - run: tide doctor"), generation)
            return
        ring = RingBuffer.with_seconds(RING_SECONDS)
        try:
            decoder.start(
                resolved_url(resolved),
                start_at=start_at,
                speed=self._speed,
                eq_gains=self._eq_gains,
                http_headers=resolved_headers(resolved),
            )
        except Exception as exc:
            self._resolve_failed(track, exc, generation)
            return

        ctx = _Context(
            track=track,
            decoder=decoder,
            ring=ring,
            start_at=start_at,
            speed=self._speed,
            generation=generation,
            source_url=resolved_url(resolved),
        )
        if not self._is_current(generation):
            decoder.stop()
            return

        with self._lock:
            self._sm.try_to(EngineState.BUFFERING)
            self._emit(EngineEvent("state", {"state": EngineState.BUFFERING.value}))
            self._ctx = ctx
            self._analyzer.reset()
            start_paused = self._pause_pending or (paused if paused is not None else self._paused)
        self._output.set_ring(ring)
        ctx.reader = threading.Thread(
            target=self._reader_loop, args=(ctx,), name=f"tide-reader-{generation}", daemon=True
        )
        ctx.reader.start()

        # Wait for preroll (or decoder EOF for short/tone sources).
        deadline = time.monotonic() + 5.0
        while not self._stopping and self._is_current(generation):
            if ring.available >= int(PREROLL_SECONDS * SAMPLE_RATE) * BYTES_PER_FRAME:
                break
            if ctx.eof or ctx.error:
                break
            if time.monotonic() > deadline:
                break
            time.sleep(0.01)

        if not self._is_current(generation):
            ctx.abandoned = True
            decoder.stop()
            return

        with self._lock:
            if ctx.error:
                self._fail(ctx.error or "decoder failed", generation)
                return
            self._paused = bool(start_paused)
            self._pause_pending = False
            if self._paused:
                self._sm.try_to(EngineState.PAUSED)
            else:
                started = self._output.start()
                if started or self._output.status == "no_device":
                    self._sm.try_to(EngineState.PLAYING)
                else:
                    self._sm.try_to(EngineState.PLAYING)  # silent mode still advances
            self._emit(EngineEvent("state", {"state": self._sm.state.value}))
            self._schedule_prefetch_locked()

    def _resolve_failed(self, track: Track, exc: Exception, generation: int) -> None:
        if not self._is_current(generation):
            return
        message = classify_error(exc, track.title or track.source)
        with self._lock:
            self._error_position = self._position_locked()
            self._error = message
            self._can_retry = True
            self._sm.try_to(EngineState.ERROR)
            self._emit(EngineEvent("state", {"state": EngineState.ERROR.value}))
            self._emit(EngineEvent("error", {"message": message, "track": track, "retryable": True}))

    def _resolve(self, track: Track) -> Any:
        if _is_local_source(track.source):
            path = Path(track.source.removeprefix("file://")).expanduser()
            if not path.is_file():
                raise FileNotFoundError(f"file not found: {path}")
            return _Resolved(str(path), None, None)
        resolver = self._resolver
        if resolver is None:
            from tide.media.resolve import resolve_stream

            config = self.config
            if config is None:
                from tide.config import load_config

                config = load_config().config
            resolver = lambda source: resolve_stream(source, config)  # noqa: E731
        return resolver(track.source)

    # -- reader ----------------------------------------------------------
    def _reader_loop(self, ctx: _Context) -> None:
        log.debug("reader started for generation %d", ctx.generation)
        while not self._stopping and not ctx.abandoned:
            data = ctx.decoder.read(READ_CHUNK)
            if not data:
                ctx.eof = True
                if ctx.decoder.returncode not in (0, None) and not ctx.abandoned:
                    tail = ctx.decoder.stderr_tail()
                    ctx.error = tail or f"ffmpeg exited with code {ctx.decoder.returncode}"
                    log.error("decoder failed: %s", ctx.error)
                break
            if ctx is self._ctx:
                self._feed_analyzer(data)
            view = memoryview(data)
            while view and not ctx.abandoned and not self._stopping:
                written = ctx.ring.write(view, block=True, timeout=0.1)
                if not written:
                    # Ring full (e.g. paused): backpressure, try again shortly.
                    time.sleep(0.02)
                view = view[written:]
            if ctx.abandoned:
                break
        log.debug("reader stopped for generation %d", ctx.generation)

    def _feed_analyzer(self, data: bytes) -> None:
        import numpy as np

        try:
            samples = np.frombuffer(data, dtype=np.float32)
        except ValueError:  # pragma: no cover - misaligned tail
            return
        usable = samples.size - samples.size % CHANNELS
        if usable == 0:
            return
        frames = samples[:usable].reshape(-1, CHANNELS)
        for frame in self._analyzer.feed(frames):
            self._emit(
                EngineEvent("frame", {"frame": frame}),
            )

    # -- tick ------------------------------------------------------------
    def _tick(self) -> None:
        spawn_track: Track | None = None
        swap_now = False
        end_now = False
        with self._lock:
            if self._stopping:
                return
            ctx = self._ctx
            if ctx is not None and ctx.eof and ctx.ring.available == 0:
                if self._staging is not None and self._staging.generation >= self._generation:
                    swap_now = True
                elif not self._staging_pending:
                    index = self._next_index_locked(force=False)
                    if index is None:
                        end_now = True
                    else:
                        self._index = index
                        spawn_track = self._queue[index]
                        self._staging_pending = True
            self._check_stall_locked()
            state = self._sm.state
            now = time.monotonic()
            if state in (EngineState.PLAYING, EngineState.BUFFERING) and now - self._last_position_emit >= POSITION_EVENT_SECONDS:
                self._last_position_emit = now
                self._emit(
                    EngineEvent(
                        "position",
                        {"position": self._position_locked(), "duration": self.duration()},
                    )
                )

        # Heavy work runs outside the lock: network resolution never blocks
        # snapshot() or UI commands.
        if swap_now:
            with self._lock:
                self._swap_to_staging_locked()
        elif end_now:
            with self._lock:
                self._end_locked()
        elif spawn_track is not None:
            threading.Thread(
                target=self._staging_worker,
                args=(spawn_track,),
                name="tide-staging",
                daemon=True,
            ).start()

    def _staging_worker(self, track: Track) -> None:
        """Resolve + spawn the next decoder into the staging ring (no lock held)."""
        try:
            resolved = self._resolve(track)
        except Exception as exc:
            with self._lock:
                self._staging_pending = False
                if self._sm.state in (EngineState.PLAYING, EngineState.BUFFERING, EngineState.ENDED):
                    self._error = classify_error(exc, track.title or track.source)
                    self._can_retry = True
                    self._error_position = self._position_locked()
                    self._sm.try_to(EngineState.ERROR)
                    self._emit(EngineEvent("state", {"state": EngineState.ERROR.value}))
                    self._emit(
                        EngineEvent(
                            "error", {"message": self._error, "track": track, "retryable": True}
                        )
                    )
            return
        with self._lock:
            if self._stopping or self._ctx is None or self._staging is not None:
                self._staging_pending = False
                return
            if self._prefetched is not None and self._prefetched[0] == track.source:
                resolved = self._prefetched[1]
            self._spawn_staging_locked(track, resolved)
            self._staging_pending = False

    def _swap_to_staging_locked(self) -> None:
        staging = self._staging
        if staging is None:
            return
        old = self._ctx
        self._staging = None
        self._ctx = staging
        self._generation = staging.generation
        if old is not None:
            old.abandoned = True
            if old.reader is not None:
                threading.Thread(target=old.reader.join, kwargs={"timeout": 1.0}, daemon=True).start()
            old.decoder.stop()
        self._analyzer.reset()
        self._output.set_ring(staging.ring)
        if not self._paused:
            self._output.start()
            self._sm.try_to(EngineState.PLAYING)
        else:
            self._sm.try_to(EngineState.PAUSED)
        self._emit(EngineEvent("state", {"state": self._sm.state.value}))
        self._emit(
            EngineEvent(
                "track",
                {"track": staging.track, "index": self._index, "queue": tuple(self._queue), "gapless": True},
            )
        )
        log.debug("gapless swap to '%s'", staging.track.title)
        self._schedule_prefetch_locked()

    def _spawn_staging_locked(self, track: Track, resolved: Any) -> None:
        generation = self._generation + 1
        try:
            decoder = FFmpegDecoder(self._ffmpeg_path())
            decoder.start(
                resolved_url(resolved),
                start_at=0.0,
                speed=self._speed,
                eq_gains=self._eq_gains,
                http_headers=resolved_headers(resolved),
            )
        except Exception as exc:  # pragma: no cover - spawn failure
            self._fail(f"could not start decoder: {exc}")
            return
        ring = RingBuffer.with_seconds(RING_SECONDS)
        ctx = _Context(
            track=track,
            decoder=decoder,
            ring=ring,
            start_at=0.0,
            speed=self._speed,
            generation=generation,
            source_url=resolved_url(resolved),
        )
        self._staging = ctx
        ctx.reader = threading.Thread(
            target=self._reader_loop, args=(ctx,), name=f"tide-staging-{generation}", daemon=True
        )
        ctx.reader.start()
        self._prefetched = None

    def _end_locked(self) -> None:
        self._sm.try_to(EngineState.ENDED)
        self._output.stop()
        self._paused = False
        self._emit(EngineEvent("state", {"state": EngineState.ENDED.value}))

    def _fail(self, message: str, generation: int | None = None) -> None:
        if generation is not None and not self._is_current(generation):
            return
        with self._lock:
            self._error = message
            self._can_retry = True
            self._sm.try_to(EngineState.ERROR)
            self._emit(EngineEvent("state", {"state": EngineState.ERROR.value}))
            self._emit(EngineEvent("error", {"message": message, "retryable": True}))

    # -- stall / buffering ----------------------------------------------
    def _check_stall_locked(self) -> None:
        ctx = self._ctx
        if ctx is None:
            return
        state = self._sm.state
        if state == EngineState.PLAYING and not ctx.eof and not self._paused:
            if ctx.ring.available_seconds < STALL_SECONDS and ctx.error is None:
                self._sm.try_to(EngineState.BUFFERING)
                self._emit(EngineEvent("state", {"state": EngineState.BUFFERING.value}))
        elif state == EngineState.BUFFERING and not self._paused:
            if ctx.error:
                self._fail(ctx.error)
            elif ctx.ring.available_seconds >= RECOVER_SECONDS or ctx.eof:
                self._sm.try_to(EngineState.PLAYING)
                self._emit(EngineEvent("state", {"state": EngineState.PLAYING.value}))

    # -- prefetch --------------------------------------------------------
    def _schedule_prefetch_locked(self) -> None:
        if self._prefetching or self._staging is not None:
            return
        ctx = self._ctx
        if ctx is None or self._repeat == "one":
            return
        remaining = None
        if ctx.track.duration:
            remaining = float(ctx.track.duration) - ctx.position
        if remaining is not None and remaining > PREFETCH_EARLY_SECONDS:
            return
        index = self._next_index_locked(force=False)
        if index is None or not (0 <= index < len(self._queue)):
            return
        track = self._queue[index]
        if self._prefetched is not None and self._prefetched[0] == track.source:
            return
        self._prefetching = True
        generation = self._generation

        def worker() -> None:
            try:
                resolved = None if _is_local_source(track.source) else self._resolve(track)
                if resolved is None:
                    resolved = _Resolved(track.source, None, None)
                if self._is_current(generation):
                    with self._lock:
                        self._prefetched = (track.source, resolved)
            except Exception as exc:
                log.debug("prefetch failed for %s: %s", track.source, exc)
            finally:
                with self._lock:
                    self._prefetching = False

        threading.Thread(target=worker, name="tide-prefetch", daemon=True).start()

    # -- helpers ---------------------------------------------------------
    def _pause_fade(self) -> None:
        self._output.fade_to(0.0, 0.3)
        time.sleep(0.32)
        with self._lock:
            should_stop = self._sm.state == EngineState.PAUSED and self._paused
        if should_stop:
            self._output.stop()

    def _is_current(self, generation: int) -> bool:
        with self._lock:
            return generation == self._generation and not self._stopping

    def _abandon_locked(self) -> None:
        ctx, staging = self._ctx, self._staging
        self._ctx = None
        self._staging = None
        self._output.set_ring(None)
        for context in (ctx, staging):
            if context is None:
                continue
            context.abandoned = True
            context.decoder.stop()
            if context.reader is not None and context.reader is not threading.current_thread():
                threading.Thread(target=context.reader.join, kwargs={"timeout": 0.5}, daemon=True).start()

    def _ffmpeg_path(self) -> Path | str | None:
        config = self.config
        return getattr(config, "ffmpeg_path", None) if config is not None else None

    def _emit(self, event: EngineEvent) -> None:
        self._events.append(event)

    def _emit_state(self) -> None:
        self._emit(EngineEvent("state", {"state": self._sm.state.value}))

    def _output_event(self, kind: str, data: dict) -> None:
        with self._lock:
            self._emit(EngineEvent(kind, data))

    def _build_session_state(self) -> dict[str, Any] | None:
        from tide.store.session import SessionState

        ctx = self._ctx
        state = SessionState(
            track=ctx.track.to_dict() if ctx else None,
            position=self._position_locked(),
            queue=[t.to_dict() for t in self._queue],
            index=self._index,
            volume=self._output.volume,
            muted=self._output.muted,
            shuffle=self._shuffle,
            repeat=self._repeat,
            speed=self._speed,
            eq_preset=self._eq_preset,
            eq_gains=self._eq_gains,
        )
        return state.to_dict()


# ---------------------------------------------------------------------------
# Resolver result helpers (duck-typed for tests and media layer)
# ---------------------------------------------------------------------------
@dataclass
class _Resolved:
    url: str
    http_headers: dict[str, str] | None
    duration: int | None


def resolved_url(resolved: Any) -> str:
    if isinstance(resolved, str):
        return resolved
    return str(getattr(resolved, "url", resolved))


def resolved_headers(resolved: Any) -> dict[str, str] | None:
    if resolved is None:
        return None
    headers = getattr(resolved, "http_headers", None)
    return dict(headers) if headers else None


# ---------------------------------------------------------------------------
# Error classification (imported by the media layer and the UI)
# ---------------------------------------------------------------------------
def classify_error(exc: BaseException, title: str = "") -> str:
    """One sentence: what happened, what to do next (no emoji, no jargon)."""
    from tide.media.errors import classify_yt_dlp_error

    if isinstance(exc, FileNotFoundError):
        return f"{title or 'That file'} is gone. Check the path, then press r to retry."
    if isinstance(exc, TimeoutError):
        return "The network timed out. Check your connection, then press r to retry."
    text = str(exc)
    if text:
        classified = classify_yt_dlp_error(text)
        if classified:
            return classified
    return f"Could not play {title or 'the track'}: {exc.__class__.__name__}. Press r to retry."
