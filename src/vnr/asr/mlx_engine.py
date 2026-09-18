"""MLX speech-to-text engine — in-process, Apple Silicon (docs/PLAN.md §5, §6).

Kyutai's designated on-device path. Unlike the subprocess adapter there is no child
process and no stdin: the model lives in this process, on the Metal GPU, and audio frames
are stepped through it directly. That keeps §19 unchanged — the UI still captures PCM and
sends it over loopback — while removing a moving part.

The model is loaded once and stays resident; a session only resets the generator state.

Inference is synchronous and blocking, so it runs on one dedicated worker thread. The
event loop is never blocked, and MLX only ever sees calls from that single thread.

The moshi_mlx calls themselves live behind :class:`SttBackend` so everything around them —
frame buffering, partial emission, finalize drain, cancellation — is testable on any
machine. The real backend is exercised only on Apple Silicon.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import queue
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..config import AsrConfig
from ..errors import AsrUnavailableError
from ..events import EventEmitter, EventType
from ..logging import get_logger, log
from .engine import AsrEngine

logger = get_logger("asr.mlx")

#: Text tokens that carry no transcript content (padding / end-of-stream).
SKIP_TOKENS = (0, 3)
#: SentencePiece word-boundary marker.
WORD_BOUNDARY = "▁"
#: Upstream pairs 4-bit with group_size 32 and 8-bit with 64.
GROUP_SIZE = {4: 32, 8: 64}


class SttBackend(Protocol):
    """The blocking inference surface. One implementation is real, one is a test double."""

    def load(self) -> None: ...
    def reset(self) -> None: ...
    def step(self, frame: bytes) -> str: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class ModelPaths:
    config: Path
    weights: Path
    mimi: Path
    tokenizer: Path
    pytorch_weights: bool


def resolve_paths(config: AsrConfig) -> ModelPaths:
    """Find the four files the runtime needs, locally or from the Hub.

    ``config.json`` is authoritative: it names the Mimi weights, the LM weights and the
    tokenizer, so we never guess filenames.
    """
    if config.model_dir:
        directory = Path(config.model_dir)
        if not directory.is_dir():
            raise AsrUnavailableError(f"ASR model directory not found: {directory}")
        raw = _read_config(directory / "config.json")
        weights_name = config.weights_name or raw.get("moshi_name") or "model.safetensors"
        paths = ModelPaths(
            config=directory / "config.json",
            weights=directory / weights_name,
            mimi=directory / str(raw["mimi_name"]),
            tokenizer=directory / str(raw["tokenizer_name"]),
            pytorch_weights=config.pytorch_weights,
        )
        for path in (paths.weights, paths.mimi, paths.tokenizer):
            if not path.exists():
                raise AsrUnavailableError(
                    f"{path.name} is named in {paths.config} but missing from {directory}"
                )
        return paths

    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise AsrUnavailableError(
            "huggingface_hub is not installed. Run: uv pip install -e '.[mlx]'"
        ) from exc

    repo = config.hf_repo
    config_path = Path(hf_hub_download(repo, "config.json"))
    raw = _read_config(config_path)
    weights_name = config.weights_name or raw.get("moshi_name") or "model.safetensors"
    return ModelPaths(
        config=config_path,
        weights=Path(hf_hub_download(repo, weights_name)),
        mimi=Path(hf_hub_download(repo, str(raw["mimi_name"]))),
        tokenizer=Path(hf_hub_download(repo, str(raw["tokenizer_name"]))),
        # The -candle repos ship PyTorch-layout weights that need a different loader.
        pytorch_weights=config.pytorch_weights or repo.endswith("-candle"),
    )


def _read_config(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise AsrUnavailableError(f"config.json not found at {path}") from exc
    except json.JSONDecodeError as exc:
        raise AsrUnavailableError(f"{path} is not valid JSON: {exc.msg}") from exc
    missing = [key for key in ("mimi_name", "tokenizer_name") if key not in raw]
    if missing:
        raise AsrUnavailableError(f"{path} does not declare {', '.join(missing)}")
    return raw


@dataclass(frozen=True)
class Quantization:
    """Bit width *and when quantization happens* — which are not the same decision."""

    bits: int
    group_size: int
    #: True when the checkpoint on disk is itself quantized.
    quantize_before_load: bool

    def describe(self) -> str:
        when = "on disk" if self.quantize_before_load else "at load"
        return f"{self.bits}-bit (group {self.group_size}), quantized {when}"


def _on_disk_bits(weights: Path) -> int | None:
    """Quantized MLX checkpoints are published as ``*.q4.safetensors`` / ``*.q8.safetensors``."""
    for bits in GROUP_SIZE:
        if weights.name.endswith(f".q{bits}.safetensors"):
            return bits
    return None


def plan_quantization(weights: Path, config: AsrConfig) -> Quantization | None:
    """Decide the quantization, including the ordering it implies.

    Two cases that look alike and are opposites:

    * **Quantized on disk** (``*.q4``/``*.q8``). ``nn.quantize`` must run *before*
      ``load_weights``, so the module tree holds QuantizedLinear/QuantizedEmbedding slots
      for the ``.scales`` and ``.biases`` the file carries.
    * **bf16 plus VNR_ASR_QUANT_BITS.** The reverse: load the weights, *then* convert.
      Quantizing first makes the tree demand ``.scales``/``.biases`` that a bf16 file does
      not contain, and ``load_weights(strict=True)`` fails naming every one of them.

    The checkpoint's own format is a fact, so it wins over the env var when they disagree —
    you cannot load 8-bit weights into a 4-bit tree.
    """
    on_disk = _on_disk_bits(weights)
    if on_disk is not None:
        if config.quant_bits and config.quant_bits != on_disk:
            log(
                logger,
                logging.WARNING,
                "checkpoint quantization overrides VNR_ASR_QUANT_BITS",
                requested_bits=config.quant_bits,
                weights=weights.name,
                using_bits=on_disk,
            )
        return Quantization(on_disk, GROUP_SIZE[on_disk], quantize_before_load=True)

    if config.quant_bits:
        bits = config.quant_bits
        if bits not in GROUP_SIZE:
            raise AsrUnavailableError(
                f"VNR_ASR_QUANT_BITS must be 4 or 8 (or unset), got {bits}"
            )
        return Quantization(bits, GROUP_SIZE[bits], quantize_before_load=False)

    return None


@dataclass(frozen=True)
class MlxModules:
    """The third-party modules the backend drives.

    Injectable so the load sequence — above all *when* ``nn.quantize`` runs relative to
    ``load_weights`` — can be tested off Apple Silicon. That ordering is the one piece of
    this file the fake-backend tests structurally cannot reach.
    """

    mx: Any
    nn: Any
    models: Any
    utils: Any
    rustymimi: Any
    sentencepiece: Any


def import_mlx_modules() -> MlxModules:
    try:
        import mlx.core as mx
        import mlx.nn as nn
        import numpy  # noqa: F401 - required by the step path
        import rustymimi
        import sentencepiece
        from moshi_mlx import models, utils
    except ImportError as exc:
        raise AsrUnavailableError(
            f"The MLX speech stack is not installed ({exc.name}). On Apple Silicon run:\n"
            "  uv pip install -e '.[mlx]'\n"
            "It is Apple-Silicon only — there is no MLX build for other platforms.",
            user_message="Speech recognition is not installed.",
        ) from exc
    return MlxModules(
        mx=mx,
        nn=nn,
        models=models,
        utils=utils,
        rustymimi=rustymimi,
        sentencepiece=sentencepiece,
    )


class MoshiMlxBackend:
    """The real thing. Imports are deferred so this module loads anywhere."""

    def __init__(self, config: AsrConfig, *, modules: MlxModules | None = None) -> None:
        self.config = config
        self._modules = modules
        self._model: Any = None
        self._lm_config: Any = None
        self._gen: Any = None
        self._audio_tokenizer: Any = None
        self._text_tokenizer: Any = None
        self._other_codebooks = 0
        self._mx: Any = None
        self._models: Any = None
        self._utils: Any = None
        self.quantization: Quantization | None = None

    def load(self) -> None:
        mods = self._modules or import_mlx_modules()

        paths = resolve_paths(self.config)
        raw = json.loads(paths.config.read_text())
        lm_config = mods.models.LmConfig.from_config_dict(raw)
        model = mods.models.Lm(lm_config)
        model.set_dtype(mods.mx.bfloat16)

        plan = plan_quantization(paths.weights, self.config)
        self.quantization = plan

        # Order matters and is not symmetric — see plan_quantization.
        if plan is not None and plan.quantize_before_load:
            mods.nn.quantize(model, bits=plan.bits, group_size=plan.group_size)

        try:
            if paths.pytorch_weights:
                model.load_pytorch_weights(str(paths.weights), lm_config, strict=True)
            else:
                model.load_weights(str(paths.weights), strict=True)
        except ValueError as exc:
            raise AsrUnavailableError(
                f"Loading {paths.weights.name} failed: {exc}\n"
                "If the missing parameters are all .scales/.biases, the module tree was "
                "quantized before a checkpoint that is not quantized on disk — report the "
                "weights filename, that combination is a bug here, not a config mistake.",
                user_message="The speech model could not be loaded.",
            ) from exc

        if plan is not None and not plan.quantize_before_load:
            mods.nn.quantize(model, bits=plan.bits, group_size=plan.group_size)

        self._text_tokenizer = mods.sentencepiece.SentencePieceProcessor(str(paths.tokenizer))
        self._other_codebooks = lm_config.other_codebooks
        self._audio_tokenizer = mods.rustymimi.Tokenizer(
            str(paths.mimi),
            num_codebooks=max(lm_config.generated_codebooks, lm_config.other_codebooks),
        )
        model.warmup()

        self._mx, self._models, self._utils = mods.mx, mods.models, mods.utils
        self._model, self._lm_config = model, lm_config
        self._gen = self._new_gen()
        log(
            logger,
            logging.INFO,
            "mlx weights loaded",
            weights=paths.weights.name,
            quantization=plan.describe() if plan else "none (bf16)",
        )

    def _new_gen(self) -> Any:
        return self._models.LmGen(
            model=self._model,
            max_steps=self.config.max_steps,
            text_sampler=self._utils.Sampler(top_k=25, temp=0),
            audio_sampler=self._utils.Sampler(top_k=250, temp=0.8),
            check=False,
        )

    def reset(self) -> None:
        """Start a fresh utterance without reloading any weights."""
        if self._model is None:
            return
        self._gen = self._new_gen()
        # rustymimi keeps streaming state too; reset it if this build exposes a way to.
        reset = getattr(self._audio_tokenizer, "reset", None)
        if callable(reset):
            with contextlib.suppress(Exception):
                reset()

    def step(self, frame: bytes) -> str:
        """Run one 80 ms frame through Mimi and the LM, returning any new text."""
        import numpy as np

        if self._gen is None:
            raise AsrUnavailableError("The MLX engine was stepped before it finished loading.")

        pcm = _to_float32(frame, self.config.stdin_format, np)
        block = pcm.reshape(1, 1, -1)
        codes = self._audio_tokenizer.encode_step(block)
        tokens = self._mx.array(codes).transpose(0, 2, 1)[:, :, : self._other_codebooks]
        text_token = self._gen.step(tokens[0])[0].item()
        if text_token in SKIP_TOKENS:
            return ""
        return str(self._text_tokenizer.id_to_piece(text_token)).replace(WORD_BOUNDARY, " ")

    def close(self) -> None:
        self._gen = self._model = self._audio_tokenizer = self._text_tokenizer = None


def _to_float32(frame: bytes, wire_format: str, np: Any) -> Any:
    """Mimi wants float32 in [-1, 1]; the wire carries whichever format is configured."""
    if wire_format == "f32le":
        return np.frombuffer(frame, dtype=np.float32)
    return np.frombuffer(frame, dtype=np.int16).astype(np.float32) / 32768.0


class MlxEngine(AsrEngine):
    """Drives a :class:`SttBackend` on one worker thread and emits transcript events."""

    name = "mlx"

    def __init__(self, config: AsrConfig, *, backend: SttBackend | None = None) -> None:
        super().__init__(sample_rate=config.sample_rate)
        self.config = config
        self._backend = backend or MoshiMlxBackend(config)
        self._work: queue.Queue[bytes | object] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._buffer = bytearray()
        self._transcript = ""
        self._active = False
        self._flushed: asyncio.Event | None = None
        self.load_seconds: float | None = None

    _STOP = object()
    _FLUSH = object()

    # -- lifecycle -----------------------------------------------------------------
    async def load(self) -> None:
        if self._thread is not None:
            return
        started = time.monotonic()
        # Loading is seconds of blocking work; keep the event loop responsive.
        await asyncio.to_thread(self._backend.load)
        self.load_seconds = time.monotonic() - started
        self._loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._worker, name="vnr-asr", daemon=True)
        self._thread.start()
        self._ready = True
        log(logger, logging.INFO, "mlx model resident", load_seconds=round(self.load_seconds, 3))

    async def unload(self) -> None:
        self._ready = False
        self._active = False
        if self._thread is not None:
            self._work.put(self._STOP)
            await asyncio.to_thread(self._thread.join, 10)
            self._thread = None
        self._backend.close()

    # -- session -------------------------------------------------------------------
    async def start_session(self, emitter: EventEmitter) -> None:
        if not self.ready:
            raise AsrUnavailableError("The ASR runtime is not loaded yet.")
        self._emitter = emitter
        self._buffer.clear()
        self._transcript = ""
        self._flushed = asyncio.Event()
        await asyncio.to_thread(self._backend.reset)
        self._active = True

    async def push_audio(self, frame: bytes) -> None:
        if not self._active:
            return
        # The model steps on fixed-size frames; the wire may not be framed that way.
        self._buffer.extend(frame)
        size = self.config.frame_bytes
        while len(self._buffer) >= size:
            self._work.put(bytes(self._buffer[:size]))
            del self._buffer[:size]

    async def finalize_session(self) -> str:
        """Drain the model's ~0.5 s decoding delay before declaring the transcript final."""
        if not self._active:
            return self._transcript
        silence = bytes(self.config.frame_bytes)
        for _ in range(max(1, self.config.finalize_grace_ms // self.config.frame_ms)):
            self._work.put(silence)
        self._work.put(self._FLUSH)
        if self._flushed is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._flushed.wait(), timeout=self.config.finalize_timeout_s
                )
        self._active = False
        text = self._transcript.strip()
        if self._emitter is not None:
            self._emitter.asr_final(text)
        return text

    async def cancel_session(self) -> None:
        self._active = False
        self._buffer.clear()
        while True:
            try:
                item = self._work.get_nowait()
            except queue.Empty:
                break
            if item is self._STOP:  # never swallow the shutdown signal
                self._work.put(self._STOP)
                break
        self._transcript = ""

    # -- worker thread -------------------------------------------------------------
    def _worker(self) -> None:
        while True:
            item = self._work.get()
            if item is self._STOP:
                return
            if item is self._FLUSH:
                self._post(self._mark_flushed)
                continue
            try:
                text = self._backend.step(item)  # type: ignore[arg-type]
            except Exception as exc:  # a model failure must surface, not hang the UI
                self._post(self._report_failure, str(exc))
                return
            if text:
                self._post(self._append, text)

    def _post(self, fn: Any, *args: Any) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(fn, *args)

    # -- event-loop callbacks ------------------------------------------------------
    def _append(self, text: str) -> None:
        if not self._active:
            return
        self._transcript += text
        if self._emitter is not None:
            self._emitter.asr_partial(self._transcript.strip())

    def _mark_flushed(self) -> None:
        if self._flushed is not None:
            self._flushed.set()

    def _report_failure(self, message: str) -> None:
        self._ready = False
        log(logger, logging.ERROR, "mlx step failed", error=message)
        if self._emitter is not None:
            self._emitter.emit(
                EventType.ASR_ERROR,
                code="asr_failed",
                message="Speech recognition stopped unexpectedly.",
            )
        self._mark_flushed()
