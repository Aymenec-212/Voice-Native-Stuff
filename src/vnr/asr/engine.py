"""The ASR interface every runtime adapter implements (docs/PLAN.md §6).

The interface is deliberately the one the plan names — ``start_session`` /
``push_audio`` / ``finalize_session`` / ``cancel_session`` — so a runtime can be swapped
without touching the session controller or the UI.

Two rules hold for every adapter:

1. **The model loads once.** ``load()`` is called when the local service starts and the
   weights stay resident; a session must never trigger a load.
2. **Audio never leaves the machine.** Adapters talk to a local process or an in-process
   runtime. There is no network adapter and there will not be one.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from ..events import EventEmitter


@dataclass(frozen=True)
class Transcript:
    text: str
    is_final: bool = False


class AsrEngine(abc.ABC):
    """A resident, streaming speech-recognition runtime."""

    #: Human-readable runtime name, shown in spike output and logs.
    name: str = "asr"
    #: Audio the adapter expects. Kyutai STT is 24 kHz mono (PLAN §6).
    sample_rate: int = 24_000

    def __init__(self, *, sample_rate: int = 24_000) -> None:
        self.sample_rate = sample_rate
        self._ready = False
        self._emitter: EventEmitter | None = None
        #: Set by finalize_session when the drain was cut short rather than completing.
        #: Callers must treat any timing measured across such a drain as invalid.
        self.drain_timed_out = False

    @property
    def ready(self) -> bool:
        """Recording must stay disabled until this is true (PLAN §22)."""
        return self._ready

    @abc.abstractmethod
    async def load(self) -> None:
        """Load weights. Called once at service start."""

    @abc.abstractmethod
    async def unload(self) -> None:
        """Release the runtime. Called at service shutdown."""

    @abc.abstractmethod
    async def start_session(self, emitter: EventEmitter) -> None:
        """Begin a new utterance. Partial transcripts are emitted on *emitter*."""

    @abc.abstractmethod
    async def push_audio(self, frame: bytes) -> None:
        """Feed one frame of 16-bit little-endian mono PCM at :attr:`sample_rate`."""

    @abc.abstractmethod
    async def finalize_session(self) -> str:
        """End the utterance and return the final transcript."""

    @abc.abstractmethod
    async def cancel_session(self) -> None:
        """Abandon the utterance without producing a final transcript."""

    async def __aenter__(self) -> AsrEngine:
        await self.load()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.unload()
