"""MLX engine orchestration. The moshi_mlx calls themselves only run on Apple Silicon;
everything around them — framing, worker thread, partials, drain, cancel — is tested here.
"""

import asyncio
import json
import struct

import pytest

from vnr.asr.mlx_engine import (
    MlxEngine,
    MlxModules,
    MoshiMlxBackend,
    _to_float32,
    plan_quantization,
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
    ("name", "bits", "group_size"),
    [("model.q4.safetensors", 4, 32), ("model.q8.safetensors", 8, 64)],
)
def test_a_checkpoint_quantized_on_disk_is_quantized_before_loading(
    tmp_path, name, bits, group_size
):
    plan = plan_quantization(tmp_path / name, config())
    assert (plan.bits, plan.group_size) == (bits, group_size)
    assert plan.quantize_before_load is True


def test_a_bf16_checkpoint_is_quantized_after_loading(tmp_path):
    """The regression: quantizing a bf16 tree first makes load_weights demand
    .scales/.biases the file does not contain, and it fails naming all 196 of them."""
    plan = plan_quantization(tmp_path / "model.safetensors", config(quant_bits=8))
    assert (plan.bits, plan.group_size) == (8, 64)
    assert plan.quantize_before_load is False


def test_no_quantization_when_nothing_asks_for_it(tmp_path):
    assert plan_quantization(tmp_path / "model.safetensors", config()) is None


def test_the_checkpoints_own_format_wins_over_the_env_var(tmp_path):
    """You cannot load 8-bit weights into a 4-bit tree, so the file decides."""
    plan = plan_quantization(tmp_path / "model.q8.safetensors", config(quant_bits=4))
    assert (plan.bits, plan.quantize_before_load) == (8, True)


def test_unsupported_bit_widths_are_rejected(tmp_path):
    with pytest.raises(AsrUnavailableError, match="must be 4 or 8"):
        plan_quantization(tmp_path / "model.safetensors", config(quant_bits=6))


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


# -- the load sequence, against a recording double ---------------------------------
# The FakeBackend above stands in for the whole backend, so it cannot see inside load().
# These drive the real MoshiMlxBackend.load() with every third-party module replaced by a
# recorder, which is the only way to assert the one thing that broke: call ORDER.
def recording_modules(*, load_error: Exception | None = None) -> tuple[MlxModules, list[str]]:
    from types import SimpleNamespace

    calls: list[str] = []

    class Model:
        def set_dtype(self, dtype):
            calls.append(f"set_dtype:{dtype}")

        def load_weights(self, path, strict=True):
            calls.append("load_weights")
            if load_error is not None:
                raise load_error

        def load_pytorch_weights(self, path, lm_config, strict=True):
            calls.append("load_pytorch_weights")
            if load_error is not None:
                raise load_error

        def warmup(self):
            calls.append("warmup")

    lm_config = SimpleNamespace(other_codebooks=8, generated_codebooks=8)

    def quantize(model, bits, group_size):
        calls.append(f"quantize:{bits}/{group_size}")

    models = SimpleNamespace(
        LmConfig=SimpleNamespace(from_config_dict=lambda raw: lm_config),
        Lm=lambda cfg: Model(),
        LmGen=lambda **kwargs: SimpleNamespace(step=lambda x: x),
    )
    return (
        MlxModules(
            mx=SimpleNamespace(bfloat16="bf16", array=lambda x: x),
            nn=SimpleNamespace(quantize=quantize),
            models=models,
            utils=SimpleNamespace(Sampler=lambda **kwargs: object()),
            rustymimi=SimpleNamespace(Tokenizer=lambda path, num_codebooks: object()),
            sentencepiece=SimpleNamespace(SentencePieceProcessor=lambda path: object()),
        ),
        calls,
    )


def load_with(tmp_path, *, weights: str, **config_kwargs) -> list[str]:
    directory = write_model_dir(tmp_path, moshi_name=weights)
    modules, calls = recording_modules()
    backend = MoshiMlxBackend(config(model_dir=str(directory), **config_kwargs), modules=modules)
    backend.load()
    return calls


def test_a_bf16_checkpoint_loads_first_then_quantizes(tmp_path):
    """The exact failure: quantizing first left load_weights demanding 196 .scales/.biases."""
    calls = load_with(tmp_path, weights="model.safetensors", quant_bits=8)
    assert calls.index("load_weights") < calls.index("quantize:8/64")


def test_a_quantized_checkpoint_quantizes_first_then_loads(tmp_path):
    calls = load_with(tmp_path, weights="model.q8.safetensors")
    assert calls.index("quantize:8/64") < calls.index("load_weights")


def test_nothing_is_quantized_when_no_one_asked(tmp_path):
    calls = load_with(tmp_path, weights="model.safetensors")
    assert not [c for c in calls if c.startswith("quantize")]
    assert "load_weights" in calls


def test_pytorch_layout_weights_use_the_other_loader(tmp_path):
    calls = load_with(tmp_path, weights="model.safetensors", pytorch_weights=True)
    assert "load_pytorch_weights" in calls
    assert "load_weights" not in calls


def test_the_model_is_warmed_up_after_the_weights_are_in(tmp_path):
    calls = load_with(tmp_path, weights="model.safetensors", quant_bits=4)
    assert calls.index("quantize:4/32") < calls.index("warmup")


def test_a_shape_mismatch_on_load_explains_itself(tmp_path):
    """A raw ValueError naming 196 tensors is not an actionable error message."""
    directory = write_model_dir(tmp_path, moshi_name="model.safetensors")
    error = ValueError("Missing 196 parameters: audio_embs.0.biases, audio_embs.0.scales")
    modules, _ = recording_modules(load_error=error)
    backend = MoshiMlxBackend(config(model_dir=str(directory), quant_bits=8), modules=modules)

    with pytest.raises(AsrUnavailableError) as exc:
        backend.load()

    assert ".scales/.biases" in str(exc.value)
    assert exc.value.user_message == "The speech model could not be loaded."


def test_the_plan_is_recorded_for_the_spike_to_report(tmp_path):
    directory = write_model_dir(tmp_path, moshi_name="model.safetensors")
    modules, _ = recording_modules()
    backend = MoshiMlxBackend(config(model_dir=str(directory), quant_bits=8), modules=modules)
    backend.load()
    assert backend.quantization.describe() == "8-bit (group 64), quantized at load"


# -- the finalize drain: a stall, not a total budget --------------------------------
class SlowBackend(FakeBackend):
    """Steps slowly, the way a real backlog does after a fast --file replay."""

    def __init__(self, *, delay_s: float = 0.0, stall_after: int | None = None) -> None:
        import threading

        super().__init__()
        self.delay_s = delay_s
        self.stall_after = stall_after
        #: Lets a test unwedge the worker thread, which a real stall would not.
        self.release = threading.Event()

    def step(self, frame: bytes) -> str:
        import threading

        if self.stall_after is not None and len(self.frames) >= self.stall_after:
            self.release.wait(30)
        if self.delay_s:
            threading.Event().wait(self.delay_s)
        return super().step(frame)


async def test_a_slow_backlog_is_not_a_timeout():
    """A 17 s clip replayed fast needs ~11 s of decoding after the last frame is pushed.
    Bounding total drain time turned that into a timeout reported as a real-time factor."""
    backend = SlowBackend(delay_s=0.03)
    engine = MlxEngine(AsrConfig(engine="mlx", finalize_timeout_s=0.5), backend=backend)
    await engine.load()
    await engine.start_session(EventEmitter(EventRecorder()))

    for _ in range(10):
        await engine.push_audio(speech_frame(engine))

    # 20 frames at 30 ms each is 0.6 s of work — longer than finalize_timeout_s, but
    # progress never stops, so it must complete rather than be cut short.
    final = await engine.finalize_session()

    assert engine.drain_timed_out is False
    assert final
    assert len(backend.frames) == 20  # 10 real + 10 drain frames, all stepped
    await engine.unload()


async def test_a_genuinely_stalled_model_is_reported():
    backend = SlowBackend(stall_after=2)
    engine = MlxEngine(AsrConfig(engine="mlx", finalize_timeout_s=0.3), backend=backend)
    await engine.load()
    await engine.start_session(EventEmitter(EventRecorder()))
    for _ in range(4):
        await engine.push_audio(speech_frame(engine))

    await engine.finalize_session()

    assert engine.drain_timed_out is True
    backend.release.set()
    await engine.unload()


async def test_a_cut_short_drain_invalidates_the_real_time_factor():
    """The headline metric must refuse to be a function of the timeout constant."""
    from vnr.metrics import AsrMetrics

    good = AsrMetrics(audio_seconds=17.1, decode_seconds=11.3)
    assert good.real_time_factor == pytest.approx(0.66, abs=0.01)

    cut_short = AsrMetrics(audio_seconds=17.1, decode_seconds=10.0, drain_timed_out=True)
    assert cut_short.real_time_factor is None
    assert cut_short.to_dict()["drain_timed_out"] is True


async def test_the_drain_flag_resets_between_utterances():
    backend = SlowBackend(stall_after=2)
    engine = MlxEngine(AsrConfig(engine="mlx", finalize_timeout_s=0.3), backend=backend)
    await engine.load()
    await engine.start_session(EventEmitter(EventRecorder()))
    for _ in range(4):
        await engine.push_audio(speech_frame(engine))
    await engine.finalize_session()
    assert engine.drain_timed_out is True

    backend.stall_after = None
    backend.release.set()  # a real stall would not clear; this only unwedges the double
    await engine.start_session(EventEmitter(EventRecorder()))
    await engine.push_audio(speech_frame(engine))
    await engine.finalize_session()
    assert engine.drain_timed_out is False
    await engine.unload()


# -- runtime-reported memory --------------------------------------------------------
def load_backend_with_mx(tmp_path, mx_extra: dict) -> MoshiMlxBackend:
    from dataclasses import replace

    directory = write_model_dir(tmp_path, moshi_name="model.safetensors")
    modules, _ = recording_modules()
    modules = replace(modules, mx=type("Mx", (), {"bfloat16": "bf16", **mx_extra})())
    backend = MoshiMlxBackend(config(model_dir=str(directory)), modules=modules)
    backend.load()
    return backend


def test_mlx_reports_its_own_peak_memory(tmp_path):
    """The number ru_maxrss could not make: Metal buffers MLX actually allocated."""
    backend = load_backend_with_mx(
        tmp_path, {"get_peak_memory": staticmethod(lambda: 864 * 1024**2)}
    )
    assert backend.peak_memory_mb() == pytest.approx(864.0)


def test_a_runtime_without_the_accessor_reports_nothing(tmp_path):
    """It is an MLX internal; an older build must degrade, not crash the run."""
    backend = load_backend_with_mx(tmp_path, {})
    assert backend.peak_memory_mb() is None


def test_a_failing_accessor_never_takes_the_run_down(tmp_path):
    def boom():
        raise RuntimeError("metal device gone")

    backend = load_backend_with_mx(tmp_path, {"get_peak_memory": staticmethod(boom)})
    assert backend.peak_memory_mb() is None


async def test_the_engine_delegates_to_its_backend():
    engine, _, _ = await make_engine()
    assert engine.peak_memory_mb() is None  # FakeBackend reports nothing
    await engine.unload()


def test_engines_that_cannot_report_memory_say_so():
    from vnr.asr.mock import MockAsrEngine

    assert MockAsrEngine().peak_memory_mb() is None


def test_both_memory_figures_are_kept_separate():
    from vnr.metrics import AsrMetrics

    dumped = AsrMetrics(peak_rss_mb=1034.3, model_peak_memory_mb=863.7).to_dict()
    assert dumped["peak_rss_mb"] == 1034.3
    assert dumped["model_peak_memory_mb"] == 863.7


def test_unload_drops_references_before_clearing_metal_cache(tmp_path):
    calls = []
    backend = load_backend_with_mx(
        tmp_path,
        {
            "synchronize": lambda self: calls.append("sync"),
            "clear_cache": lambda self: None,
            "get_active_memory": lambda self: 1024 if backend._model is not None else 0,
            "get_cache_memory": lambda self: 0,
            "get_peak_memory": lambda self: 2048,
        },
    )
    backend._mx.clear_cache = lambda: calls.append(
        ("clear", backend._model, backend._gen, backend._audio_tokenizer, backend._text_tokenizer)
    )
    backend.close()
    assert calls == ["sync", ("clear", None, None, None, None)]
    memory = backend.last_unload_memory
    assert memory["after"]["mlx_active_mb"] == 0
    assert memory["before"]["mlx_peak_mb"] == memory["after"]["mlx_peak_mb"]


async def test_engine_can_transcribe_again_after_unload():
    backend = FakeBackend()
    engine = MlxEngine(config(), backend=backend)
    for _ in range(2):
        await engine.load()
        await engine.start_session(EventEmitter(EventRecorder()))
        await engine.push_audio(speech_frame(engine))
        assert await engine.finalize_session()
        await engine.unload()
        assert not engine.ready
        assert engine._thread is None


def test_cache_limit_and_warm_release_preserve_weights(tmp_path):
    calls = []
    backend = load_backend_with_mx(
        tmp_path,
        {
            "set_cache_limit": lambda self, value: calls.append(("limit", value)),
            "clear_cache": lambda self: calls.append("clear"),
            "synchronize": lambda self: calls.append("sync"),
        },
    )
    assert calls == [("limit", 128 * 2**20), "clear"]
    model = backend._model
    from types import SimpleNamespace

    model.transformer_cache = [SimpleNamespace(reset=lambda: calls.append("reset KV"))]
    model.depformer_cache = [SimpleNamespace(reset=lambda: calls.append("reset depformer"))]
    backend.release_session()
    assert backend._model is model
    assert backend._gen is None
    assert calls[-4:] == ["reset KV", "reset depformer", "sync", "clear"]
    backend.reset()
    assert backend._model is model and backend._gen is not None
    assert calls[-2:] == ["reset KV", "reset depformer"]


async def test_reset_inference_and_release_share_one_worker_thread():
    import threading

    class Tracked(FakeBackend):
        threads = []
        releases = 0

        def reset(self):
            self.threads.append(threading.get_ident())
            super().reset()

        def step(self, frame):
            self.threads.append(threading.get_ident())
            return super().step(frame)

        def release_session(self):
            self.threads.append(threading.get_ident())
            self.releases += 1

    backend = Tracked()
    engine = MlxEngine(config(), backend=backend)
    await engine.load()
    for _ in range(2):
        await engine.start_session(EventEmitter(EventRecorder()))
        await engine.push_audio(speech_frame(engine))
        assert await engine.finalize_session()
        assert engine.ready and not backend.closed
    assert backend.releases >= 2
    assert len(set(backend.threads)) == 1
    await engine.unload()
