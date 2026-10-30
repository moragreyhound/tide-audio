"""Real-time audio analysis: FFT bands, spectral flux, beat detection.

Pipeline per hop (window 2048, hop 512, Hann, mono mix):

1. rFFT of the windowed frame, magnitude normalized so a full-scale sine
   reads 1.0;
2. bins mapped to 64 log-spaced bands (spectrum) and 16 bands (ambient wash);
3. perceptual tilt (gentle high-frequency lift) and smoothing with a fast
   attack / slow release envelope;
4. spectral flux over log magnitudes with an adaptive threshold (moving
   average times a multiplier) producing a beat flag;
5. an immutable :class:`AnalysisFrame` per hop, including RMS and a
   decimated waveform for the oscilloscope.

The analyzer never blocks and never allocates unbounded memory: it keeps one
window of samples plus fixed-size history buffers.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np

WINDOW = 2048
HOP = 512
N_BANDS = 64
N_WASH = 16
WAVE_POINTS = 256

FLUX_HISTORY = 48       # ~0.5 s of hops at 48 kHz / 512
FLUX_MULTIPLIER = 1.45
FLUX_BIAS = 0.004
BEAT_MIN_INTERVAL = 0.11  # seconds; refractory period
MIN_HISTORY = 3          # hops before beats are reported


@dataclass(frozen=True, slots=True)
class AnalysisFrame:
    """Immutable per-hop analysis result."""

    index: int
    time: float
    bands: tuple[float, ...]     # 64 values, roughly 0..1
    wash: tuple[float, ...]      # 16 values, roughly 0..1
    flux: float
    beat: bool
    rms: float                   # 0..1
    wave: tuple[float, ...]      # decimated mono samples, -1..1


def log_band_bins(
    n_fft: int, sample_rate: int, n_bands: int, f_min: float = 20.0, f_max: float | None = None
) -> np.ndarray:
    """Map log-spaced frequency edges to ``(n_bands + 1,)`` rFFT bin indices.

    Edges are strictly non-decreasing; each band owns at least one bin.
    """
    nyquist = sample_rate / 2.0
    top = min(f_max or (nyquist * 0.95), nyquist)
    top = max(top, f_min * 2)
    edges = np.geomspace(f_min, top, n_bands + 1)
    bin_freqs = np.fft.rfftfreq(n_fft, d=1.0 / sample_rate)
    idx = np.searchsorted(bin_freqs, edges).astype(np.int64)
    idx = np.clip(idx, 0, len(bin_freqs) - 1)
    for i in range(1, len(idx)):
        if idx[i] <= idx[i - 1]:
            idx[i] = min(idx[i - 1] + 1, len(bin_freqs) - 1)
    return idx


class Analyzer:
    """Stateful streaming analyzer. Feed PCM, receive :class:`AnalysisFrame`s."""

    def __init__(
        self,
        sample_rate: int = 48000,
        window: int = WINDOW,
        hop: int = HOP,
        n_bands: int = N_BANDS,
        n_wash: int = N_WASH,
    ) -> None:
        self.sample_rate = int(sample_rate)
        self.window = int(window)
        self.hop = int(hop)
        self.n_bands = int(n_bands)
        self.n_wash = int(n_wash)

        self._hann = np.hanning(self.window).astype(np.float64)
        self._band_idx = log_band_bins(self.window, self.sample_rate, self.n_bands)
        self._wash_idx = log_band_bins(self.window, self.sample_rate, self.n_wash)

        self._norm = 2.0 / self.window  # rFFT magnitude scaling: sine peak -> 1.0
        self._freqs = np.fft.rfftfreq(self.window, d=1.0 / self.sample_rate)
        self._centers = np.array(
            [
                self._freqs[self._band_idx[i] : max(self._band_idx[i + 1], self._band_idx[i] + 1)].mean()
                for i in range(self.n_bands)
            ]
        )
        self._tilt = np.clip((self._centers / 1000.0) ** 0.15, 0.5, 3.0)

        self._log_mag: np.ndarray | None = None
        self._smooth = np.zeros(self.n_bands, dtype=np.float64)
        self._wash_smooth = np.zeros(self.n_wash, dtype=np.float64)
        self._flux_hist: deque[float] = deque(maxlen=FLUX_HISTORY)
        self._buf = np.zeros(0, dtype=np.float64)
        self._frames_emitted = 0
        self._samples_consumed = 0
        self._last_beat_time = -1.0

    # -- introspection (used by tests) -----------------------------------
    @property
    def band_bins(self) -> np.ndarray:
        return self._band_idx.copy()

    @property
    def wash_bins(self) -> np.ndarray:
        return self._wash_idx.copy()

    def reset(self) -> None:
        self._log_mag = None
        self._smooth[:] = 0.0
        self._wash_smooth[:] = 0.0
        self._flux_hist.clear()
        self._buf = np.zeros(0, dtype=np.float64)
        self._frames_emitted = 0
        self._samples_consumed = 0
        self._last_beat_time = -1.0

    # -- feeding ---------------------------------------------------------
    def feed(self, samples: np.ndarray) -> list[AnalysisFrame]:
        """Consume a block of PCM (mono ``(n,)`` or stereo ``(n, 2)``).

        Returns every full hop produced by this block.
        """
        data = np.asarray(samples, dtype=np.float64)
        if data.ndim == 2:
            data = data.mean(axis=1)
        elif data.ndim != 1:
            raise ValueError(f"expected 1-D or 2-D samples, got shape {data.shape}")

        if data.size == 0:
            return []
        self._buf = np.concatenate([self._buf, data])
        frames: list[AnalysisFrame] = []
        while self._buf.size >= self.window:
            frame = self._buf[: self.window]
            start_sample = self._samples_consumed
            frames.append(self._process(frame, start_sample))
            self._buf = self._buf[self.hop :]
            self._samples_consumed += self.hop
        return frames

    # -- internals -------------------------------------------------------
    def _process(self, frame: np.ndarray, start_sample: int) -> AnalysisFrame:
        windowed = frame * self._hann
        spectrum = np.fft.rfft(windowed) * self._norm
        magnitude = np.abs(spectrum)
        log_mag = np.log10(magnitude + 1e-9)

        if self._log_mag is None:
            flux = 0.0
        else:
            diff = log_mag - self._log_mag
            diff[diff < 0] = 0.0
            flux = float(diff.sum() / (len(diff) or 1))
        self._log_mag = log_mag

        power = magnitude * magnitude
        rms = float(np.sqrt(np.mean(frame * frame)))

        bands = self._band_values(power, self._band_idx, self._tilt)
        self._smooth = _envelope(self._smooth, bands, attack=0.60, release=0.12)

        wash_raw = self._band_values(power, self._wash_idx, None)
        self._wash_smooth = _envelope(self._wash_smooth, wash_raw, attack=0.35, release=0.05)

        # Adaptive beat threshold over recent flux history.
        self._flux_hist.append(flux)
        beat = False
        time_s = start_sample / self.sample_rate
        if len(self._flux_hist) >= MIN_HISTORY:
            mean = float(np.mean(self._flux_hist))
            threshold = mean * FLUX_MULTIPLIER + FLUX_BIAS
            if flux > threshold and (time_s - self._last_beat_time) >= BEAT_MIN_INTERVAL:
                beat = True
                self._last_beat_time = time_s

        idx = np.linspace(0, self.window - 1, WAVE_POINTS).astype(np.int64)
        wave = frame[idx]

        return AnalysisFrame(
            index=self._frames_emitted,
            time=time_s,
            bands=tuple(float(v) for v in self._smooth),
            wash=tuple(float(v) for v in self._wash_smooth),
            flux=flux,
            beat=beat,
            rms=rms,
            wave=tuple(float(v) for v in wave),
        )

    def _band_values(
        self, power: np.ndarray, edges: np.ndarray, tilt: np.ndarray | None
    ) -> np.ndarray:
        out = np.empty(len(edges) - 1, dtype=np.float64)
        for i in range(len(edges) - 1):
            lo = int(edges[i])
            hi = max(int(edges[i + 1]), lo + 1)
            segment = power[lo:hi]
            energy = float(np.sqrt(segment.mean())) if segment.size else 0.0
            if tilt is not None:
                energy *= float(tilt[i])
            db = 20.0 * np.log10(energy + 1e-9)
            out[i] = min(1.0, max(0.0, (db + 72.0) / 60.0))
        return out


def _envelope(state: np.ndarray, values: np.ndarray, *, attack: float, release: float) -> np.ndarray:
    """Fast attack, slow release smoothing (both coefficients in 0..1)."""
    rising = values > state
    coef = np.where(rising, attack, release)
    return state + (values - state) * coef


def beat_strength(frame: AnalysisFrame) -> float:
    """Normalized strength of the last transient (0 when quiet)."""
    return min(1.0, frame.flux * 8.0)
