"""ASR layer: stream parsing, the subprocess adapter, audio sources, engine selection."""

import asyncio
import struct
import sys
import wave
from pathlib import Path

import pytest

from vnr.asr.audio import AudioError, SilenceSource, WavFileSource, _downmix, _to_wire
from vnr.asr.mock import MockAsrEngine
from vnr.asr.moshicpp import MoshiCppEngine
from vnr.asr.registry import create_engine
from vnr.asr.streamparse import JsonLineAccumulator, TextStreamAccumulator
from vnr.config import AsrConfig
from vnr.errors import AsrUnavailableError, ConfigError
from vnr.events import EventEmitter, EventRecorder, EventType

FAKE = Path(__file__).parent / "fake_stt.py"


def fake_config(extra: str = "", **overrides) -> AsrConfig:
    defaults = dict(
        engine="moshicpp",
        binary=sys.executable,
        model_path="",
        sample_rate=24_000,
        frame_ms=80,
        command=f"{{binary}} {FAKE} {extra}".strip(),
        finalize_grace_ms=120,
        finalize_timeout_s=5.0,
        ready_probe_s=0.4,
    )
    defaults.update(overrides)
    return AsrConfig(**defaults)


# -- stream parsing ----------------------------------------------------------------
def test_text_accumulator_handles_appended_words():
    acc = TextStreamAccumulator()
    acc.feed("find ")
    acc.feed("recent ")
    assert acc.feed("work") == "find recent work"


def test_text_accumulator_handles_line_redraws():
    acc = TextStreamAccumulator()
    acc.feed("\rfind recent")
    assert acc.feed("\rfind recent work") == "find recent work"  # not doubled


def test_text_accumulator_commits_newlines_and_strips_ansi():
    acc = TextStreamAccumulator()
    acc.feed("\x1b[32mfirst line\x1b[0m\n")
    assert acc.feed("second") == "first line second"


def test_text_accumulator_resets_between_utterances():
    acc = TextStreamAccumulator()
    acc.feed("old words\n")
    acc.reset()
    assert acc.feed("new") == "new"


def test_json_accumulator_reads_full_transcripts_and_deltas():
    acc = JsonLineAccumulator()
    acc.feed('{"text": "find recent"}\n')
    assert acc.feed('{"text": "find recent work", "final": true}\n') == "find recent work"
    assert acc.final_seen is True

    delta = JsonLineAccumulator()
    delta.feed('{"text": "find", "delta": true}\n')
    assert delta.feed('{"text": " recent", "delta": true}\n') == "find recent"


def test_json_accumulator_survives_noise_on_stdout():
    acc = JsonLineAccumulator()
    acc.feed("loading model...\n")
    acc.feed("{not json}\n")
    assert acc.feed('{"text": "ok"}\n') == "ok"


def test_json_accumulator_waits_for_complete_lines():
    acc = JsonLineAccumulator()
    assert acc.feed('{"text": "par') == ""
    assert acc.feed('tial"}\n') == "partial"


# -- subprocess adapter ------------------------------------------------------------
async def drive(engine: MoshiCppEngine, recorder: EventRecorder, *, frames: int = 12) -> str:
    await engine.start_session(EventEmitter(recorder, session_id="test"))
    frame = bytes(engine.config.frame_bytes)
    for _ in range(frames):
        await engine.push_audio(frame)
    await asyncio.sleep(0.2)
    return await engine.finalize_session()


async def test_subprocess_engine_streams_partials_then_finalizes():
    engine = MoshiCppEngine(fake_config())
    recorder = EventRecorder()
    async with engine:
        assert engine.ready
        final = await drive(engine, recorder)

    partials = recorder.of_type(EventType.ASR_PARTIAL)
    assert partials, "the runtime should emit transcript updates while audio streams"
    assert len(partials[-1].data["text"]) >= len(partials[0].data["text"])
    assert final.startswith("find recent")
    assert recorder.of_type(EventType.ASR_FINAL)[0].data["text"] == final


async def test_model_stays_resident_across_sessions():
    """Two utterances, one process — never load-transcribe-unload (PLAN §6)."""
    engine = MoshiCppEngine(fake_config())
    async with engine:
        pid = engine._process.pid
        first = await drive(engine, EventRecorder(), frames=8)
        second = await drive(engine, EventRecorder(), frames=8)
        assert engine._process.pid == pid
    assert first and second


async def test_a_cancelled_session_drops_its_transcript():
    engine = MoshiCppEngine(fake_config())
    recorder = EventRecorder()
    async with engine:
        await engine.start_session(EventEmitter(recorder))
        for _ in range(8):
            await engine.push_audio(bytes(engine.config.frame_bytes))
        await asyncio.sleep(0.2)
        await engine.cancel_session()
        assert engine._current_text() == ""
        assert not recorder.of_type(EventType.ASR_FINAL)


async def test_redraw_output_is_not_duplicated():
    engine = MoshiCppEngine(fake_config("--mode redraw"))
    async with engine:
        final = await drive(engine, EventRecorder())
    assert final.count("find") == 1


async def test_json_output_format():
    engine = MoshiCppEngine(fake_config("--mode json", output_format="json"))
    async with engine:
        final = await drive(engine, EventRecorder())
    assert final.startswith("find recent")


async def test_readiness_marker_is_awaited():
    config = fake_config("--ready-marker READY --load-delay 0.3", ready_marker="READY")
    engine = MoshiCppEngine(config)
    async with engine:
        assert engine.ready
        assert engine.load_seconds is not None and engine.load_seconds >= 0.3
        assert await drive(engine, EventRecorder())


async def test_a_binary_that_exits_immediately_surfaces_its_stderr():
    engine = MoshiCppEngine(fake_config("--fail"))
    with pytest.raises(AsrUnavailableError) as exc:
        await engine.load()
    assert "unrecognised option" in str(exc.value)
    await engine.unload()


async def test_missing_binary_and_model_are_named():
    with pytest.raises(AsrUnavailableError, match="VNR_ASR_BINARY is not set"):
        MoshiCppEngine(fake_config(binary="")).command()
    with pytest.raises(AsrUnavailableError, match="binary not found"):
        MoshiCppEngine(fake_config(binary="/nope/stt")).command()
    with pytest.raises(AsrUnavailableError, match="model not found"):
        MoshiCppEngine(fake_config(model_path="/nope/model.gguf")).command()


async def test_recording_is_refused_before_the_model_is_loaded():
    """PLAN §22: don't enable recording until the runtime is ready."""
    engine = MoshiCppEngine(fake_config())
    assert engine.ready is False
    with pytest.raises(AsrUnavailableError):
        await engine.start_session(EventEmitter(EventRecorder()))


def test_command_template_substitutes_paths():
    config = fake_config(
        command="{binary} --model {model} --rate {sample_rate}", model_path=__file__
    )
    argv = MoshiCppEngine(config).command()
    assert argv[0] == sys.executable
    assert argv[2] == __file__
    assert argv[4] == "24000"


# -- audio sources -----------------------------------------------------------------
def test_frame_geometry():
    config = AsrConfig(sample_rate=24_000, frame_ms=80)
    assert config.frame_samples == 1920
    assert config.frame_bytes == 3840
    assert AsrConfig(stdin_format="f32le").frame_bytes == 7680


def test_stereo_is_downmixed_and_floats_are_scaled():
    import array

    stereo = array.array("h", [100, 200, 300, 400]).tobytes()
    assert array.array("h", _downmix(stereo, 2)).tolist() == [150, 350]
    as_float = _to_wire(array.array("h", [16384]).tobytes(), "f32le")
    assert array.array("f", as_float).tolist() == [0.5]


def write_wav(path: Path, *, rate: int, channels: int = 1, seconds: float = 0.5) -> Path:
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(struct.pack("<h", 0) * int(rate * seconds) * channels)
    return path


async def test_wav_source_yields_frames_and_counts_audio(tmp_path):
    path = write_wav(tmp_path / "ok.wav", rate=24_000, seconds=0.4)
    source = WavFileSource(path, AsrConfig())
    frames = [frame async for frame in source.frames()]
    assert len(frames) == 5  # 400ms at 80ms per frame
    assert all(len(f) == 3840 for f in frames)
    assert source.seconds_captured == pytest.approx(0.4, abs=0.01)


async def test_wav_source_refuses_to_resample_silently(tmp_path):
    path = write_wav(tmp_path / "wrong.wav", rate=16_000)
    with pytest.raises(AudioError, match="16000 Hz"):
        [f async for f in WavFileSource(path, AsrConfig()).frames()]


async def test_wav_source_rejects_non_16_bit(tmp_path):
    path = tmp_path / "8bit.wav"
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(1)
        wav.setframerate(24_000)
        wav.writeframes(b"\x00" * 100)
    with pytest.raises(AudioError, match="8-bit"):
        [f async for f in WavFileSource(path, AsrConfig()).frames()]


async def test_missing_file_is_named(tmp_path):
    with pytest.raises(AudioError, match="not found"):
        [f async for f in WavFileSource(tmp_path / "nope.wav", AsrConfig()).frames()]


async def test_silence_source_respects_its_duration():
    source = SilenceSource(AsrConfig(), seconds=0.4)
    frames = [f async for f in source.frames()]
    assert len(frames) == 5


# -- engine selection --------------------------------------------------------------
def test_engine_selection():
    assert create_engine(AsrConfig(engine="mock")).name == "mock"
    assert create_engine(fake_config()).name == "moshicpp"
    with pytest.raises(ConfigError, match="Unknown VNR_ASR_ENGINE"):
        create_engine(AsrConfig(engine="mlx-bf16"))


async def test_mock_engine_streams_progressively():
    engine = MockAsrEngine(frames_per_word=1)
    recorder = EventRecorder()
    async with engine:
        await engine.start_session(EventEmitter(recorder))
        for _ in range(5):
            await engine.push_audio(b"\x00" * 3840)
        partials = [e.data["text"] for e in recorder.of_type(EventType.ASR_PARTIAL)]
        final = await engine.finalize_session()
    assert partials == sorted(partials, key=len)
    assert final.startswith("find recent work")


# -- command rendering for the real moshi.cpp layout -------------------------------
def test_default_command_points_at_the_model_directory(tmp_path):
    """moshi.cpp needs the GGUF, Mimi, the tokenizer and config.json — i.e. the dir."""
    from vnr.config import DEFAULT_ASR_COMMAND

    config = AsrConfig(
        binary=sys.executable,
        model_dir=str(tmp_path),
        quant="q4_k",
        command=DEFAULT_ASR_COMMAND,
    )
    assert MoshiCppEngine(config).command() == [
        sys.executable, "-r", str(tmp_path), "-q", "q4_k", "-i", "-"
    ]


def test_quantization_tag_is_configurable(tmp_path):
    """Falling back from q4_k to q8_0 must not need a code change."""
    config = AsrConfig(binary=sys.executable, model_dir=str(tmp_path), quant="q8_0")
    assert "q8_0" in MoshiCppEngine(config).command()


def test_a_missing_model_directory_is_named(tmp_path):
    config = AsrConfig(binary=sys.executable, model_dir=str(tmp_path / "nope"))
    with pytest.raises(AsrUnavailableError, match="model directory not found"):
        MoshiCppEngine(config).command()

    unset = AsrConfig(binary=sys.executable, model_dir="")
    with pytest.raises(AsrUnavailableError, match="VNR_ASR_MODEL_DIR is not set"):
        MoshiCppEngine(unset).command()


def test_an_unknown_placeholder_names_the_valid_ones():
    config = AsrConfig(binary=sys.executable, command="{binary} --weights {checkpoint}")
    with pytest.raises(ConfigError, match="unknown placeholder 'checkpoint'"):
        config.render_command()


# -- real-time factor --------------------------------------------------------------
def test_real_time_factor_is_decode_wall_time_over_audio():
    from vnr.metrics import AsrMetrics

    metrics = AsrMetrics(audio_seconds=10.0, decode_seconds=4.0)
    assert metrics.real_time_factor == 0.4
    # Undefined rather than misleading when a live mic makes the ratio meaningless.
    assert AsrMetrics(audio_seconds=10.0).real_time_factor is None
    assert AsrMetrics(decode_seconds=4.0).real_time_factor is None
