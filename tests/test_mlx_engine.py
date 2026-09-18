"""MLX engine orchestration. The moshi_mlx calls themselves only run on Apple Silicon;
everything around them — framing, worker thread, partials, drain, cancel — is tested here.
"""

import asyncio
import json
import struct

import pytest

from vnr.asr.mlx_engine import (
    MlxEngine,
    _to_float32,
    quantization_for,
    resolve_paths,
)
from vnr.config import AsrConfig
from vnr.errors import AsrUnavailableError
from vnr.events import EventEmitter, EventRecorder, EventType


class FakeBackend:
    """Emits one scripted word per frame, so frame accounting is observable."""

    WORDS = ["▁find", "▁recent", "▁work", "▁on", "▁Darija"]

    def __init__(self, *, fail_on: int | None = None) -> None:
        self.loaded = False
        self.resets = 0
        self.frames: list[bytes] = []
        self.closed = False
        self.fail_on = fail_on

    def load(self) -> None:
        self.loaded = True

    def reset(self) -> None:
        self.resets += 1

    def step(self, frame: bytes) -> str:
        self.frames.append(frame)
        if self.fail_on is not None and len(self.frames) >= self.fail_on:
            raise RuntimeError("metal out of memory")
        if any(frame):  # silence decodes to nothing, like the real model
            return self.WORDS[(len(self.frames) - 1) % len(self.WORDS)].replace("▁", " ")
        return ""

    def close(self) -> None:
        self.closed = True


def config(**overrides) -> AsrConfig:
    return AsrConfig(engine="mlx", **overrides)


async def make_engine(**kwargs) -> tuple[MlxEngine, FakeBackend, EventRecorder]:
    backend = FakeBackend(**kwargs)
    engine = MlxEngine(config(), backend=backend)
    await engine.load()
    return engine, backend, EventRecorder()


def speech_frame(engine: MlxEngine) -> bytes:
    return b"\x01\x02" * (engine.config.frame_bytes // 2)


async def settle() -> None:
    """Let the worker thread's callbacks land on the loop."""
    for _ in range(50):
        await asyncio.sleep(0.005)


# -- streaming ---------------------------------------------------------------------
async def test_partials_accumulate_while_audio_streams():
    engine, backend, recorder = await make_engine()
    await engine.start_session(EventEmitter(recorder, session_id="t"))

    for _ in range(3):
        await engine.push_audio(speech_frame(engine))
    await settle()

    partials = [e.data["text"] for e in recorder.of_type(EventType.ASR_PARTIAL)]
    assert partials == ["find", "find recent", "find recent work"]
    await engine.unload()


async def test_the_wire_is_reframed_to_the_model_frame_size():
    """The UI may send any chunk size; the model only ever sees 80 ms frames."""
    engine, backend, recorder = await make_engine()
    await engine.start_session(EventEmitter(recorder))

    frame = engine.config.frame_bytes
    await engine.push_audio(b"\x01\x02" * (frame // 2 + frame // 4))  # 1.5 frames
    await settle()
    assert len(backend.frames) == 1, "a partial frame must wait for the rest"

    await engine.push_audio(b"\x01\x02" * (frame // 4))  # completes the second
    await settle()
    assert len(backend.frames) == 2
    assert {len(f) for f in backend.frames} == {frame}
    await engine.unload()


async def test_finalize_drains_the_models_delay_then_emits_final():
    engine, backend, recorder = await make_engine()
    await engine.start_session(EventEmitter(recorder))
    await engine.push_audio(speech_frame(engine))
    await settle()

    final = await engine.finalize_session()

    # 800 ms of grace at 80 ms per frame = 10 silent frames pushed through the model.
    silent = [f for f in backend.frames if not any(f)]
    assert len(silent) == 10
    assert final == "find"
    assert recorder.of_type(EventType.ASR_FINAL)[0].data["text"] == "find"
    await engine.unload()


async def test_the_model_stays_resident_across_sessions():
    engine, backend, _ = await make_engine()
    for _ in range(3):
        await engine.start_session(EventEmitter(EventRecorder()))
        await engine.push_audio(speech_frame(engine))
        await settle()
        await engine.finalize_session()

    assert backend.resets == 3, "each utterance resets generator state"
    assert backend.loaded and not backend.closed, "weights were never reloaded"
    await engine.unload()
    assert backend.closed


async def test_a_new_session_starts_from_an_empty_transcript():
    engine, _, _ = await make_engine()
    await engine.start_session(EventEmitter(EventRecorder()))
    await engine.push_audio(speech_frame(engine))
    await settle()
    assert await engine.finalize_session() == "find"

    recorder = EventRecorder()
    await engine.start_session(EventEmitter(recorder))
    await engine.push_audio(speech_frame(engine))
    await settle()
    # One frame in, so exactly one word — the previous utterance did not carry over.
    first = recorder.of_type(EventType.ASR_PARTIAL)[0].data["text"]
    assert first and len(first.split()) == 1
    await engine.unload()


# -- lifecycle and failure ---------------------------------------------------------
async def test_recording_is_refused_before_load():
    engine = MlxEngine(config(), backend=FakeBackend())
    assert engine.ready is False
    with pytest.raises(AsrUnavailableError):
        await engine.start_session(EventEmitter(EventRecorder()))


async def test_audio_outside_a_session_is_ignored():
    engine, backend, _ = await make_engine()
    await engine.push_audio(speech_frame(engine))
    await settle()
    assert backend.frames == []
    await engine.unload()


async def test_cancel_drops_the_transcript_and_pending_audio():
    engine, _, recorder = await make_engine()
    await engine.start_session(EventEmitter(recorder))
    for _ in range(3):
        await engine.push_audio(speech_frame(engine))

    await engine.cancel_session()
    await settle()

    assert not recorder.of_type(EventType.ASR_FINAL)
    await engine.start_session(EventEmitter(recorder))
    await engine.push_audio(speech_frame(engine))
    await settle()
    assert recorder.of_type(EventType.ASR_PARTIAL)[-1].data["text"].count("find") <= 1
    await engine.unload()


async def test_a_backend_failure_surfaces_instead_of_hanging():
    engine, _, recorder = await make_engine(fail_on=2)
    await engine.start_session(EventEmitter(recorder, session_id="t"))
    for _ in range(3):
        await engine.push_audio(speech_frame(engine))
    await settle()

    errors = recorder.of_type(EventType.ASR_ERROR)
    assert errors and errors[0].data["code"] == "asr_failed"
    assert engine.ready is False, "a dead model must stop the UI offering to record"
    # finalize must return rather than block on a worker that has exited
    assert await asyncio.wait_for(engine.finalize_session(), timeout=12) is not None
    await engine.unload()


# -- pure helpers ------------------------------------------------------------------
def test_pcm_conversion():
    import numpy as np

    s16 = struct.pack("<3h", 0, 16384, -32768)
    assert _to_float32(s16, "s16le", np).tolist() == [0.0, 0.5, -1.0]
    f32 = struct.pack("<2f", 0.25, -0.5)
    assert _to_float32(f32, "f32le", np).tolist() == [0.25, -0.5]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("model.q4.safetensors", (4, 32)),
        ("model.q8.safetensors", (8, 64)),
        ("model.safetensors", None),
    ],
)
def test_quantization_is_inferred_from_the_weights_filename(tmp_path, name, expected):
    assert quantization_for(tmp_path / name, config()) == expected


def test_explicit_quantization_bits_win(tmp_path):
    assert quantization_for(tmp_path / "model.safetensors", config(quant_bits=8)) == (8, 64)
    assert quantization_for(tmp_path / "model.q8.safetensors", config(quant_bits=4)) == (4, 32)
    with pytest.raises(AsrUnavailableError, match="must be 4 or 8"):
        quantization_for(tmp_path / "model.safetensors", config(quant_bits=6))


def write_model_dir(tmp_path, **overrides):
    payload = {
        "mimi_name": "mimi.safetensors",
        "moshi_name": "model.q8.safetensors",
        "tokenizer_name": "tok.model",
        **overrides,
    }
    (tmp_path / "config.json").write_text(json.dumps(payload))
    for key in ("mimi_name", "moshi_name", "tokenizer_name"):
        if payload.get(key):
            (tmp_path / payload[key]).write_text("x")
    return tmp_path


def test_local_paths_come_from_config_json(tmp_path):
    directory = write_model_dir(tmp_path)
    paths = resolve_paths(config(model_dir=str(directory)))
    assert paths.weights.name == "model.q8.safetensors"
    assert paths.mimi.name == "mimi.safetensors"
    assert paths.tokenizer.name == "tok.model"
    assert paths.pytorch_weights is False


def test_weights_name_can_be_overridden(tmp_path):
    directory = write_model_dir(tmp_path)
    (directory / "model.q4.safetensors").write_text("x")
    paths = resolve_paths(config(model_dir=str(directory), weights_name="model.q4.safetensors"))
    assert paths.weights.name == "model.q4.safetensors"


def test_a_file_named_but_missing_is_reported_clearly(tmp_path):
    directory = write_model_dir(tmp_path)
    (directory / "mimi.safetensors").unlink()
    with pytest.raises(AsrUnavailableError, match="named in .* but missing"):
        resolve_paths(config(model_dir=str(directory)))


def test_a_config_without_the_required_names_is_rejected(tmp_path):
    (tmp_path / "config.json").write_text(json.dumps({"moshi_name": "m.safetensors"}))
    with pytest.raises(AsrUnavailableError, match="does not declare"):
        resolve_paths(config(model_dir=str(tmp_path)))


def test_a_missing_model_directory_is_reported(tmp_path):
    with pytest.raises(AsrUnavailableError, match="model directory not found"):
        resolve_paths(config(model_dir=str(tmp_path / "nope")))
