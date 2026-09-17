"""A scripted ASR engine so the harness, the event path and the UI can be built and
tested on any machine — including CI, which has no microphone and no model weights.

It is not a fallback for the real runtime: it invents text, and the spike labels it
loudly. Nothing about Milestone 1 can be validated with it (PLAN §5).
"""

from __future__ import annotations

import asyncio

from ..events import EventEmitter
from .engine import AsrEngine

DEFAULT_SCRIPT = (
    "find recent work on streaming ASR for low-resource languages "
    "and tell me what might apply to Moroccan Darija"
)


class MockAsrEngine(AsrEngine):
    name = "mock"

    def __init__(
        self,
        *,
        sample_rate: int = 24_000,
        script: str = DEFAULT_SCRIPT,
        frames_per_word: int = 2,
        load_delay_s: float = 0.0,
    ) -> None:
        super().__init__(sample_rate=sample_rate)
        self._words = script.split()
        self._frames_per_word = max(1, frames_per_word)
        self._load_delay_s = load_delay_s
        self._frames = 0
        self._active = False

    async def load(self) -> None:
        if self._load_delay_s:
            await asyncio.sleep(self._load_delay_s)
        self._ready = True

    async def unload(self) -> None:
        self._ready = False

    async def start_session(self, emitter: EventEmitter) -> None:
        self._emitter = emitter
        self._frames = 0
        self._active = True

    async def push_audio(self, frame: bytes) -> None:  # noqa: ARG002 - length is irrelevant
        if not self._active:
            return
        self._frames += 1
        if self._frames % self._frames_per_word:
            return
        text = self._text()
        if text and self._emitter is not None:
            self._emitter.asr_partial(text)

    async def finalize_session(self) -> str:
        text = self._text(complete=True)
        self._active = False
        if self._emitter is not None:
            self._emitter.asr_final(text)
        return text

    async def cancel_session(self) -> None:
        self._active = False
        self._frames = 0

    def _text(self, *, complete: bool = False) -> str:
        if complete:
            return " ".join(self._words)
        spoken = min(len(self._words), self._frames // self._frames_per_word)
        return " ".join(self._words[:spoken])
