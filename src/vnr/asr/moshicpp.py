"""Adapter for a local ggml/moshi.cpp-style streaming STT binary (docs/PLAN.md §5).

This is the runtime that actually loads the Q4_K GGUF quantization of
``efficient-nlp/stt-1b-en_fr-quantized``. The binary is spawned **once** and kept
resident; each utterance is a session over its stdin/stdout, so weights are never
reloaded per request.

The command line is configuration, not a hardcoded guess: set ``VNR_ASR_COMMAND`` to
whatever the binary you built actually accepts. The spike prints the exact command it
runs, and surfaces the child's stderr when it exits early, so a wrong flag is obvious
within a second.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shlex
import time
from collections import deque
from pathlib import Path

from ..config import AsrConfig
from ..errors import AsrUnavailableError
from ..events import EventEmitter
from ..logging import get_logger, log
from .engine import AsrEngine
from .streamparse import JsonLineAccumulator, TextStreamAccumulator

logger = get_logger("asr.moshicpp")

READ_CHUNK = 256


class MoshiCppEngine(AsrEngine):
    """Streams PCM into a long-lived local transcriber process."""

    name = "moshicpp"

    def __init__(self, config: AsrConfig) -> None:
        super().__init__(sample_rate=config.sample_rate)
        self.config = config
        self._process: asyncio.subprocess.Process | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_tail: deque[str] = deque(maxlen=40)
        self._accumulator = self._make_accumulator()
        self._active = False
        self._last_output_at = 0.0
        self.load_seconds: float | None = None

    # -- lifecycle -----------------------------------------------------------------
    def command(self) -> list[str]:
        """The argv this engine will run. Validated so failures name the missing piece."""
        if not self.config.binary:
            raise AsrUnavailableError(
                "VNR_ASR_BINARY is not set — build the streaming STT binary and point "
                "VNR_ASR_BINARY at it (see docs/milestone-1-asr.md).",
                user_message="Speech recognition is not configured.",
            )
        if not Path(self.config.binary).exists():
            raise AsrUnavailableError(f"ASR binary not found: {self.config.binary}")
        if self.config.model_path and not Path(self.config.model_path).exists():
            raise AsrUnavailableError(f"ASR model not found: {self.config.model_path}")
        if "{model_dir}" in self.config.command and not self.config.model_dir:
            raise AsrUnavailableError(
                "VNR_ASR_MODEL_DIR is not set — point it at the directory holding the "
                "GGUF, the Mimi weights, the tokenizer and config.json."
            )
        if self.config.model_dir and not Path(self.config.model_dir).is_dir():
            raise AsrUnavailableError(f"ASR model directory not found: {self.config.model_dir}")
        return shlex.split(self.config.render_command())

    async def load(self) -> None:
        """Spawn the process once and wait until it reports readiness."""
        if self._process is not None:
            return
        argv = self.command()
        started = time.monotonic()
        log(logger, logging.INFO, "starting asr process", argv=argv)
        try:
            self._process = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise AsrUnavailableError(f"Could not start {argv[0]!r}: {exc}") from exc

        self._stderr_task = asyncio.create_task(self._drain_stderr())
        await self._await_ready()
        self._reader_task = asyncio.create_task(self._read_stdout())
        self.load_seconds = time.monotonic() - started
        self._ready = True
        log(logger, logging.INFO, "asr process ready", load_seconds=round(self.load_seconds, 3))

    async def unload(self) -> None:
        self._ready = False
        for task in (self._reader_task, self._stderr_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._reader_task = self._stderr_task = None
        process = self._process
        self._process = None
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                process.kill()

    # -- session -------------------------------------------------------------------
    async def start_session(self, emitter: EventEmitter) -> None:
        if not self.ready:
            raise AsrUnavailableError("The ASR runtime is not loaded yet.")
        self._emitter = emitter
        self._accumulator.reset()
        self._active = True
        self._last_output_at = time.monotonic()

    async def push_audio(self, frame: bytes) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise AsrUnavailableError("The ASR runtime is not running.")
        if not self._active:
            return
        try:
            process.stdin.write(frame)
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise AsrUnavailableError(
                f"The ASR process stopped accepting audio: {exc}\n{self.stderr_tail()}"
            ) from exc

    async def finalize_session(self) -> str:
        """Stop feeding audio and wait out the model's decoding delay (PLAN §5).

        Kyutai STT runs about half a second behind the audio, so the tail of an utterance
        arrives after the microphone has already closed. We wait for output to go quiet
        rather than cutting the transcript off mid-sentence.
        """
        self.drain_timed_out = False
        deadline = time.monotonic() + self.config.finalize_timeout_s
        quiet_for = self.config.finalize_grace_ms / 1000.0
        while True:
            if time.monotonic() - self._last_output_at >= quiet_for:
                break
            if time.monotonic() >= deadline:
                # Still producing output when the budget ran out: the transcript is
                # truncated, so anything timed across this drain is not a measurement.
                self.drain_timed_out = True
                log(logger, logging.WARNING, "finalize drain cut short")
                break
            await asyncio.sleep(0.05)
        self._active = False
        text = self._current_text()
        if self._emitter is not None:
            self._emitter.asr_final(text)
        return text

    async def cancel_session(self) -> None:
        self._active = False
        self._accumulator.reset()

    def stderr_tail(self) -> str:
        return "\n".join(self._stderr_tail)

    # -- internals -----------------------------------------------------------------
    def _make_accumulator(self) -> TextStreamAccumulator | JsonLineAccumulator:
        return (
            JsonLineAccumulator()
            if self.config.output_format == "json"
            else TextStreamAccumulator()
        )

    def _current_text(self) -> str:
        return self._accumulator.text

    async def _await_ready(self) -> None:
        """Wait for the readiness marker, or for the process to survive startup."""
        process = self._process
        assert process is not None and process.stdout is not None
        marker = self.config.ready_marker
        if not marker:
            # No marker configured: give the model time to load, and fail loudly if the
            # process dies first (a bad flag shows up here as its own usage message).
            try:
                await asyncio.wait_for(process.wait(), timeout=self.config.ready_probe_s)
            except TimeoutError:
                return  # still running after the probe — treat as ready
            raise AsrUnavailableError(
                f"The ASR process exited immediately (code {process.returncode}).\n"
                f"Command: {' '.join(self.command())}\n{self.stderr_tail()}"
            )

        deadline = asyncio.get_running_loop().time() + self.config.ready_timeout_s
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise AsrUnavailableError(
                    f"Timed out waiting for {marker!r} from the ASR process.\n"
                    f"{self.stderr_tail()}"
                )
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=remaining)
            except TimeoutError:
                continue
            if not line:
                raise AsrUnavailableError(
                    f"The ASR process closed its output before signalling readiness.\n"
                    f"{self.stderr_tail()}"
                )
            if marker in line.decode("utf-8", "replace"):
                return

    async def _read_stdout(self) -> None:
        process = self._process
        assert process is not None and process.stdout is not None
        while True:
            chunk = await process.stdout.read(READ_CHUNK)
            if not chunk:
                return
            text = self._accumulator.feed(chunk.decode("utf-8", "replace"))
            self._last_output_at = time.monotonic()
            if self._active and text and self._emitter is not None:
                self._emitter.asr_partial(text)

    async def _drain_stderr(self) -> None:
        process = self._process
        assert process is not None and process.stderr is not None
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            self._stderr_tail.append(line.decode("utf-8", "replace").rstrip())
