"""The numeric half of the slice-2 diagnosis.

A capture that sounds fine and transcribes to nothing is not a bug anyone can hear, so
these are the measurements that decide it instead. Every signal here is synthesised, so
the detectors are checked against faults whose exact shape is known — including the one
the VNRCapture file is suspected of having.
"""

from __future__ import annotations

import math
import struct
import wave
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from vnr.asr.audio import wire_to_pcm16, write_wav  # noqa: E402
from vnr.audio_analysis import (  # noqa: E402
    DC_OFFSET_LIMIT,
    HF_BAND_START_HZ,
    MIN_DISCONTINUITY_PERIOD,
    QUIET_PEAK,
    analyse,
    dominant_discontinuity,
    findings,
    read_wav,
)

RATE = 24_000


def tone(freq: float, seconds: float = 1.0, amplitude: float = 0.3, rate: int = RATE):
    t = np.arange(int(rate * seconds)) / rate
    return amplitude * np.sin(2 * math.pi * freq * t)


def speechish(seconds: float = 2.0, rate: int = RATE):
    """A few harmonics with a slow envelope — enough spectral shape to stand in for voice."""
    t = np.arange(int(rate * seconds)) / rate
    signal = sum(0.3 / n * np.sin(2 * math.pi * 140 * n * t) for n in (1, 2, 3, 5))
    return signal * (0.6 + 0.4 * np.sin(2 * math.pi * 3 * t))


def save(path: Path, samples, *, rate: int = RATE) -> Path:
    pcm = np.clip(samples, -1.0, 1.0)
    write_wav(path, (pcm * 32767).astype("<i2").tobytes(), sample_rate=rate)
    return path


# -- reading -----------------------------------------------------------------------
def test_read_wav_round_trips_rate_channels_and_values(tmp_path):
    original = tone(440, seconds=0.25)
    samples, rate, channels = read_wav(save(tmp_path / "a.wav", original))
    assert (rate, channels) == (RATE, 1)
    assert samples.size == original.size
    # 16-bit quantisation is the only loss allowed between writing and reading.
    assert np.abs(samples - original).max() < 2 / 32768


def test_stereo_is_averaged_to_mono(tmp_path):
    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(2)
        wav.setsampwidth(2)
        wav.setframerate(RATE)
        wav.writeframes(struct.pack("<hhhh", 1000, 3000, -2000, 0))
    samples, _, channels = read_wav(path)
    assert channels == 2
    assert samples.size == 2
    assert samples[0] == pytest.approx(2000 / 32768, abs=1e-6)


def test_non_16_bit_is_refused_by_name(tmp_path):
    path = tmp_path / "8bit.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(RATE)
        wav.writeframes(b"\x00" * 64)
    with pytest.raises(ValueError, match="8-bit"):
        read_wav(path)


# -- the basic statistics ----------------------------------------------------------
def test_a_clean_tone_measures_as_expected(tmp_path):
    stats = analyse(save(tmp_path / "tone.wav", tone(440, seconds=1.0, amplitude=0.5)))
    assert stats.sample_rate == RATE
    assert stats.channels == 1
    assert stats.duration_s == pytest.approx(1.0, abs=0.01)
    assert stats.peak == pytest.approx(0.5, abs=0.01)
    assert stats.rms == pytest.approx(0.5 / math.sqrt(2), abs=0.01)  # sine RMS
    assert stats.dc_offset == pytest.approx(0.0, abs=1e-3)
    assert stats.clipped_samples == 0
    # Two crossings per cycle.
    assert stats.zero_crossing_rate == pytest.approx(2 * 440 / RATE, rel=0.02)
    assert findings(stats) == []


def test_dbfs_follows_rms_and_silence_is_negative_infinity(tmp_path):
    loud = analyse(save(tmp_path / "loud.wav", tone(440, amplitude=0.5)))
    quiet = analyse(save(tmp_path / "quiet.wav", tone(440, amplitude=0.05)))
    # A tenth of the amplitude is 20 dB down, whatever the absolute numbers are.
    assert loud.dbfs - quiet.dbfs == pytest.approx(20.0, abs=0.5)
    silent = analyse(save(tmp_path / "silent.wav", np.zeros(RATE)))
    assert silent.dbfs == -math.inf


def test_clipping_is_counted(tmp_path):
    stats = analyse(save(tmp_path / "clipped.wav", tone(200, seconds=0.5, amplitude=1.4)))
    assert stats.clipped_samples > 100
    assert any("clipped" in note for note in findings(stats))


# -- the faults that sound fine ----------------------------------------------------
def test_a_dc_offset_is_reported_though_no_speaker_reproduces_it(tmp_path):
    offset = DC_OFFSET_LIMIT * 5
    stats = analyse(save(tmp_path / "dc.wav", tone(300, seconds=0.5) + offset))
    assert stats.dc_offset == pytest.approx(offset, abs=2e-3)
    assert any("DC offset" in note for note in findings(stats))


def test_a_quiet_take_is_reported_even_though_it_is_not_silence(tmp_path):
    stats = analyse(save(tmp_path / "faint.wav", tone(300, seconds=0.5, amplitude=0.01)))
    assert 0 < stats.peak < QUIET_PEAK
    assert any("very quiet" in note for note in findings(stats))


def test_high_frequency_energy_separates_voice_from_aliasing(tmp_path):
    voice = analyse(save(tmp_path / "voice.wav", speechish(seconds=1.0)))
    alias = analyse(save(tmp_path / "alias.wav", tone(HF_BAND_START_HZ + 3_000, seconds=1.0)))
    assert voice.hf_energy_ratio < 0.1
    assert alias.hf_energy_ratio > 0.9
    assert findings(voice) == []
    assert any("aliasing" in note for note in findings(alias))


def test_the_wrong_sample_rate_is_the_first_thing_said(tmp_path):
    stats = analyse(save(tmp_path / "wrong.wav", tone(300, seconds=0.5, rate=44_100), rate=44_100))
    notes = findings(stats)
    assert notes and "44100 Hz" in notes[0]
    # Same file, judged against its own rate, has nothing wrong with it.
    assert findings(stats, expected_rate=44_100) == []


# -- the periodic-discontinuity detector -------------------------------------------
def test_clean_speech_has_no_dominant_period(tmp_path):
    period, share = dominant_discontinuity(speechish(seconds=2.0))
    assert period is None
    assert share < 0.5


def test_a_per_callback_transient_is_found_at_its_exact_spacing():
    """The fault this detector exists for.

    A resampler whose state resets once per tap callback leaves a transient at every
    buffer boundary. At a 4096-frame callback and 44100 → 24000, that is one every 2229
    output samples — 92.9 ms, far too fast to hear as anything but texture.
    """
    samples = speechish(seconds=4.0)
    spacing = round(4096 * 24_000 / 44_100)
    samples[spacing::spacing] += 0.4

    period, share = dominant_discontinuity(samples)
    assert period is not None
    assert abs(period - spacing) <= 1
    assert share > 0.9


def test_the_period_is_reported_in_milliseconds(tmp_path):
    samples = speechish(seconds=4.0)
    spacing = 2_400  # exactly 100 ms at 24 kHz
    samples[spacing::spacing] += 0.4
    stats = analyse(save(tmp_path / "periodic.wav", samples))
    assert stats.discontinuity_period_ms == pytest.approx(100.0, abs=0.1)
    assert any("periodic artifact" in note for note in findings(stats))


def test_no_period_reports_no_milliseconds(tmp_path):
    stats = analyse(save(tmp_path / "clean.wav", speechish(seconds=2.0)))
    assert stats.discontinuity_period is None
    assert stats.discontinuity_period_ms is None


def spiked(period: int, *, seconds: float = 1.0, seed: int = 1):
    """Quiet noise with a large jump every *period* samples — a buffer boundary, idealised."""
    samples = 0.02 * np.random.default_rng(seed).standard_normal(int(RATE * seconds))
    samples[::period] += 0.5
    return samples


def test_a_spacing_too_short_to_be_a_callback_is_not_reported():
    """The false positive this floor exists for.

    On a real recording the detector reported "every 3 samples (0.1 ms, 56%)". Three
    samples at 24 kHz is an 8 kHz tone — ordinary brightness in a voice, not a buffer
    boundary. It fired on the known-good reference file while passing the file actually
    under suspicion, which is worse than noisy: it points the wrong way.
    """
    period, share = dominant_discontinuity(spiked(3))
    assert period is None
    assert share < 0.5


def test_the_floor_sits_between_spectral_content_and_any_real_callback():
    # No audio API delivers buffers this small, so a period here is a spectral artifact.
    assert dominant_discontinuity(spiked(MIN_DISCONTINUITY_PERIOD - 36))[0] is None
    # Just above it, the detector is live again — the floor excludes, it does not blind.
    found, share = dominant_discontinuity(spiked(MIN_DISCONTINUITY_PERIOD + 28))
    assert found is not None
    assert abs(found - (MIN_DISCONTINUITY_PERIOD + 28)) <= 1
    assert share > 0.9


def test_a_real_callback_period_still_registers_inside_a_bright_file():
    """The floor must not buy its silence by going deaf.

    A file can be both bright and broken — high-frequency content near the floor *and* a
    genuine transient every buffer. Suppressing the first must leave the second visible.
    """
    rng = np.random.default_rng(3)
    samples = speechish(seconds=4.0) + 0.06 * rng.standard_normal(int(RATE * 4))
    spacing = round(4096 * 24_000 / 44_100)
    samples[spacing::spacing] += 0.6

    period, share = dominant_discontinuity(samples)
    assert period is not None
    assert abs(period - spacing) <= 1
    assert share >= 0.5


def test_too_short_to_judge_says_so_rather_than_guessing():
    assert dominant_discontinuity(np.zeros(16)) == (None, 0.0)
    # Digital silence has no spread, so no jump can be unusual.
    assert dominant_discontinuity(np.zeros(4_000)) == (None, 0.0)


def test_an_empty_file_is_an_error_not_a_zeroed_report(tmp_path):
    with pytest.raises(ValueError, match="no samples"):
        analyse(save(tmp_path / "empty.wav", np.zeros(0)))


# -- the wire round trip, which is what --dump-wav relies on -----------------------
def test_s16le_frames_pass_through_the_dump_untouched():
    pcm = struct.pack("<hhh", 1000, -1000, 32767)
    assert wire_to_pcm16(pcm, "s16le") == pcm


def test_f32le_frames_come_back_within_one_quantisation_step():
    import array

    original = array.array("h", [0, 1000, -1000, 32767, -32768])
    floats = array.array("f", (s / 32768.0 for s in original)).tobytes()
    restored = array.array("h")
    restored.frombytes(wire_to_pcm16(floats, "f32le"))
    for before, after in zip(original, restored, strict=True):
        assert abs(before - after) <= 2


def test_out_of_range_floats_clamp_rather_than_wrap():
    import array

    floats = array.array("f", [2.0, -2.0]).tobytes()
    restored = array.array("h")
    restored.frombytes(wire_to_pcm16(floats, "f32le"))
    assert restored.tolist() == [32767, -32767]


# -- the CLI ------------------------------------------------------------------------
def test_the_diff_reports_a_clean_file_as_clean(tmp_path, capsys):
    from vnr.cli.audio_diff import main

    path = save(tmp_path / "clean.wav", speechish(seconds=1.0))
    assert main([str(path)]) == 0
    assert "nothing here explains a failure to transcribe" in capsys.readouterr().out


def test_the_diff_names_the_file_with_the_fault(tmp_path, capsys):
    from vnr.cli.audio_diff import main

    clean = save(tmp_path / "clean.wav", speechish(seconds=4.0))
    broken_samples = speechish(seconds=4.0)
    spacing = round(4096 * 24_000 / 44_100)
    broken_samples[spacing::spacing] += 0.4
    broken = save(tmp_path / "broken.wav", broken_samples)

    # Non-zero because something was found — usable as a check, not just a printout.
    assert main([str(clean), str(broken)]) == 1

    out = capsys.readouterr().out
    assert "periodic jumps" in out
    assert "none detected" in out
    assert "92.8 ms" in out
    assert "Differences:" in out


def test_a_missing_file_is_an_error_not_a_traceback(tmp_path, capsys):
    from vnr.cli.audio_diff import main

    assert main([str(tmp_path / "nope.wav")]) == 2
    assert "error:" in capsys.readouterr().err


def test_the_expected_rate_is_selectable(tmp_path, capsys):
    from vnr.cli.audio_diff import main

    path = save(tmp_path / "raw.wav", speechish(seconds=1.0, rate=44_100), rate=44_100)
    assert main([str(path)]) == 1                      # wrong for the model
    assert main([str(path), "--expect-rate", "44100"]) == 0   # right for a raw dump


# -- the gain stage ---------------------------------------------------------------
def test_gain_of_one_returns_the_very_same_bytes():
    from vnr.asr.audio import apply_gain

    frame = struct.pack("<3h", 100, -200, 300)
    # Identity, not merely equality: the default path must not rewrite the audio at all.
    assert apply_gain(frame, "s16le", 1.0) is frame


def test_gain_scales_int16_samples():
    import array

    from vnr.asr.audio import apply_gain

    out = array.array("h")
    out.frombytes(apply_gain(struct.pack("<3h", 1000, -1000, 0), "s16le", 4.0))
    assert out.tolist() == [4000, -4000, 0]


def test_gain_clamps_instead_of_wrapping():
    """The failure this guards against turns a loud syllable into its own negative."""
    import array

    from vnr.asr.audio import apply_gain

    out = array.array("h")
    out.frombytes(apply_gain(struct.pack("<2h", 20_000, -20_000), "s16le", 8.0))
    assert out.tolist() == [32767, -32768]


def test_gain_clamps_floats_too():
    import array

    from vnr.asr.audio import apply_gain

    out = array.array("f")
    out.frombytes(apply_gain(array.array("f", [0.3, -0.3]).tobytes(), "f32le", 10.0))
    assert out.tolist() == [1.0, -1.0]


def test_an_empty_frame_survives_any_gain():
    from vnr.asr.audio import apply_gain

    assert apply_gain(b"", "s16le", 9.0) == b""
