"""Measurements that separate audio a human calls fine from audio a model can read.

A recording can sound correct — right pitch, right speed, right length — and still
produce zero tokens. The differences that matter here are ones the ear forgives: a DC
offset it cannot reproduce, aliasing it hears as faint hiss, or a periodic discontinuity
it hears as texture. Each of those is plainly visible in numbers.

Pure functions over sample arrays, so the interesting parts are testable without audio
hardware or model weights.
"""

from __future__ import annotations

import contextlib
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Speech above this is normal; below it a take is probably too quiet to recognise even
#: though it is plainly not silence.
QUIET_PEAK = 0.05
#: A DC offset this large is inaudible but shifts every sample the model sees.
DC_OFFSET_LIMIT = 0.01
#: Energy above this fraction of Nyquist, as a share of total. A correct 24 kHz speech
#: recording puts little energy up here; a bad resampler folds aliases into it.
HF_BAND_START_HZ = 6_000


@dataclass(frozen=True)
class AudioStats:
    path: str
    sample_rate: int
    channels: int
    sample_count: int
    duration_s: float
    peak: float
    rms: float
    dc_offset: float
    clipped_samples: int
    zero_crossing_rate: float
    hf_energy_ratio: float
    #: Dominant spacing, in samples, of large sample-to-sample jumps — the signature of a
    #: resampler or buffer boundary repeating at a fixed period. None when no period
    #: stands out.
    discontinuity_period: int | None
    discontinuity_share: float

    @property
    def dbfs(self) -> float:
        import math

        return -math.inf if self.rms <= 0 else 20 * math.log10(self.rms)

    @property
    def discontinuity_period_ms(self) -> float | None:
        if self.discontinuity_period is None:
            return None
        return 1000.0 * self.discontinuity_period / self.sample_rate


def read_wav(path: Path | str) -> tuple[Any, int, int]:
    """Read a 16-bit PCM WAV into float samples in [-1, 1]. Returns (samples, rate, ch)."""
    import numpy as np

    path = Path(path)
    with contextlib.closing(wave.open(str(path), "rb")) as wav:
        if wav.getsampwidth() != 2:
            raise ValueError(f"{path.name}: {wav.getsampwidth() * 8}-bit; 16-bit expected")
        rate = wav.getframerate()
        channels = wav.getnchannels()
        raw = wav.readframes(wav.getnframes())
    samples = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)
    return samples, rate, channels


def dominant_discontinuity(
    samples: Any, *, threshold_sigma: float = 6.0
) -> tuple[int | None, float]:
    """Find a repeating spacing between unusually large sample-to-sample jumps.

    A resampler whose state resets once per callback leaves a transient at every buffer
    boundary. The ear hears texture; the spacing is exact and shows up here as one
    dominant gap. Returns ``(period_in_samples, share_of_jumps_at_that_period)``.
    """
    import numpy as np

    if samples.size < 64:
        return None, 0.0
    jumps = np.abs(np.diff(samples))
    spread = jumps.std()
    if spread <= 0:
        return None, 0.0
    spikes = np.flatnonzero(jumps > jumps.mean() + threshold_sigma * spread)
    if spikes.size < 8:
        return None, 0.0

    gaps = np.diff(spikes)
    gaps = gaps[gaps > 1]
    if gaps.size < 6:
        return None, 0.0

    # Tolerate ±1 sample: a fractional resampling ratio makes the period wobble.
    values, counts = np.unique(gaps, return_counts=True)
    best_period, best_count = None, 0
    for value in values:
        near = counts[np.abs(values - value) <= 1].sum()
        if near > best_count:
            best_period, best_count = int(value), int(near)
    share = best_count / gaps.size
    # Below half, there is no single period — just ordinary speech transients.
    return (best_period, share) if share >= 0.5 else (None, share)


def analyse(path: Path | str) -> AudioStats:
    import numpy as np

    samples, rate, channels = read_wav(path)
    if samples.size == 0:
        raise ValueError(f"{Path(path).name} contains no samples")

    peak = float(np.abs(samples).max())
    rms = float(np.sqrt(np.mean(samples**2)))
    dc_offset = float(samples.mean())
    clipped = int(np.count_nonzero(np.abs(samples) >= 32767 / 32768))
    crossings = int(np.count_nonzero(np.diff(np.signbit(samples))))

    # Welch-ish: one FFT over the whole signal is enough to see a gross spectral tilt.
    spectrum = np.abs(np.fft.rfft(samples * np.hanning(samples.size))) ** 2
    freqs = np.fft.rfftfreq(samples.size, d=1.0 / rate)
    total = spectrum.sum()
    hf_ratio = float(spectrum[freqs >= HF_BAND_START_HZ].sum() / total) if total > 0 else 0.0

    period, share = dominant_discontinuity(samples)

    return AudioStats(
        path=str(path),
        sample_rate=rate,
        channels=channels,
        sample_count=int(samples.size),
        duration_s=samples.size / rate,
        peak=peak,
        rms=rms,
        dc_offset=dc_offset,
        clipped_samples=clipped,
        zero_crossing_rate=crossings / samples.size,
        hf_energy_ratio=hf_ratio,
        discontinuity_period=period,
        discontinuity_share=share,
    )


def findings(stats: AudioStats, *, expected_rate: int = 24_000) -> list[str]:
    """Things that would stop this file transcribing, worst first."""
    notes: list[str] = []
    if stats.sample_rate != expected_rate:
        notes.append(
            f"sample rate is {stats.sample_rate} Hz, not {expected_rate} — the spike "
            "rejects anything else, and the model is trained at 24 kHz"
        )
    if stats.discontinuity_period is not None:
        notes.append(
            f"large jumps repeat every {stats.discontinuity_period} samples "
            f"({stats.discontinuity_period_ms:.1f} ms, {stats.discontinuity_share:.0%} of "
            "them) — a periodic artifact, which is what a resampler or buffer boundary "
            "resetting once per callback looks like. Mostly inaudible; not to a model"
        )
    if abs(stats.dc_offset) > DC_OFFSET_LIMIT:
        notes.append(
            f"DC offset {stats.dc_offset:+.4f} — speakers cannot reproduce it so it "
            "sounds fine, but every sample the model sees is shifted"
        )
    if stats.peak < QUIET_PEAK:
        notes.append(f"peak {stats.peak:.3f} is very quiet")
    if stats.clipped_samples:
        notes.append(f"{stats.clipped_samples} clipped samples")
    if stats.hf_energy_ratio > 0.5:
        notes.append(
            f"{stats.hf_energy_ratio:.0%} of the energy is above {HF_BAND_START_HZ} Hz — "
            "far more than speech, which suggests aliasing or noise rather than voice"
        )
    return notes
