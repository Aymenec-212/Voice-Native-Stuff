"""The session controller — the state machine behind the whole experience.

It owns the transitions in docs/PLAN.md §18 and enforces the product's two hardest rules:

* **Recording end never starts research.** ``recording.stop`` lands in ``REVIEW`` and
  waits there. Only an explicit ``research.submit`` moves on (PLAN §2.2, §7).
* **Only approved text leaves the machine.** The controller keeps ``raw_transcript`` and
  ``submitted_query`` apart, and the research service is handed the second one.

It is transport-agnostic on purpose: the WebSocket service drives it, the terminal
prototype drives it, and tests drive it directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from .asr.audio import SILENCE_THRESHOLD, peak_amplitude
from .asr.engine import AsrEngine
from .config import Settings
from .errors import AsrUnavailableError, MicrophoneError, VnrError
from .events import EventEmitter, EventSink, SessionState, null_sink
from .logging import get_logger, log
from .research.agent import ResearchResult
from .research.runner import run_research
from .session import ResearchSession

logger = get_logger("controller")

#: Which commands are legal in which state. Anything else is refused with a reason,
#: because a UI bug must not be able to start a search the user did not approve.
#: A new utterance is fine from any settled state — after an answer, after a failure,
#: after a cancel. It is refused only while something is already in flight.
SETTLED = frozenset(
    {
        SessionState.IDLE,
        SessionState.REVIEW,
        SessionState.COMPLETED,
        SessionState.FAILED,
        SessionState.CANCELLED,
    }
)

ALLOWED: dict[str, frozenset[SessionState]] = {
    "recording.start": SETTLED,
    "audio.frame": frozenset({SessionState.LISTENING}),
    "recording.stop": frozenset({SessionState.LISTENING}),
    # FAILED is included so a provider error can be retried on the same approved text
    # without re-recording (PLAN §22).
    "research.submit": frozenset({SessionState.REVIEW, SessionState.FAILED}),
}


class CommandRejected(VnrError):
    code = "rejected"
    user_message = "That is not possible right now."


@dataclass
class ControllerDeps:
    """Seams for tests: the research call is injected rather than imported inline."""

    research: Callable[..., Awaitable[tuple[ResearchSession, ResearchResult]]] = run_research


class SessionController:
    def __init__(
        self,
        engine: AsrEngine,
        settings: Settings,
        *,
        sink: EventSink = null_sink,
        deps: ControllerDeps | None = None,
    ) -> None:
        self._engine = engine
        self._settings = settings
        self._sink = sink
        self._deps = deps or ControllerDeps()
        self.session = ResearchSession()
        self._emitter = EventEmitter(sink, session_id=self.session.id)
        self._state = SessionState.IDLE
        self._research_task: asyncio.Task[None] | None = None
        self._frames = 0
        self._input_peak = 0.0

    # -- state ---------------------------------------------------------------------
    @property
    def state(self) -> SessionState:
        return self._state

    @property
    def raw_transcript(self) -> str:
        return self.session.raw_transcript

    def _transition(self, state: SessionState) -> None:
        self._state = state
        self.session.status = state
        self._emitter.state_changed(state)

    def _require(self, command: str) -> None:
        allowed = ALLOWED.get(command)
        if allowed is not None and self._state not in allowed:
            raise CommandRejected(
                f"{command} is not allowed in {self._state.value}",
                user_message=f"Cannot {command.split('.')[-1]} while {self._state.value.lower()}.",
            )

    # -- commands ------------------------------------------------------------------
    async def start_recording(self) -> None:
        """Begin an utterance. Refused until the ASR runtime is resident (PLAN §22)."""
        self._require("recording.start")
        if not self._engine.ready:
            raise AsrUnavailableError(
                "The speech model is still loading.",
                user_message="Speech recognition is not ready yet.",
            )
        # A new utterance starts a new session: the previous answer is history.
        self.session = ResearchSession()
        self._emitter = EventEmitter(self._sink, session_id=self.session.id)
        self._frames = 0
        self._input_peak = 0.0
        await self._engine.start_session(self._emitter)
        self._transition(SessionState.LISTENING)

    async def push_audio(self, frame: bytes) -> None:
        self._require("audio.frame")
        self._frames += 1
        self.session.asr_metrics.audio_seconds += self._settings.asr.frame_ms / 1000.0
        self._input_peak = max(
            self._input_peak, peak_amplitude(frame, self._settings.asr.stdin_format)
        )
        await self._engine.push_audio(frame)

    async def stop_recording(self) -> str:
        """End the utterance and land in REVIEW. This never starts research."""
        self._require("recording.stop")
        self._transition(SessionState.FINALIZING_TRANSCRIPT)
        try:
            transcript = await self._engine.finalize_session()
        except VnrError:
            self._transition(SessionState.FAILED)
            raise
        self.session.raw_transcript = transcript
        self.session.asr_metrics.partial_count = self._frames
        self.session.asr_metrics.input_peak = self._input_peak

        if not transcript and self._frames and self._input_peak < SILENCE_THRESHOLD:
            # macOS hands an app without microphone permission a stream of zeros rather
            # than an error, and it silently routes capture to a connected Bluetooth
            # headset. Both look identical to a working run that heard nothing, so say
            # which it was instead of returning an empty transcript.
            self._transition(SessionState.FAILED)
            raise MicrophoneError(
                f"captured {self._frames} frames of digital silence",
                user_message=(
                    "The microphone delivered only silence. Check System Settings → "
                    "Privacy & Security → Microphone, and which input device is selected "
                    "— a connected headset is often picked automatically."
                ),
            )

        self._transition(SessionState.REVIEW)
        log(
            logger,
            logging.INFO,
            "awaiting approval",
            chars=len(transcript),
            input_peak=round(self._input_peak, 4),
        )
        return transcript

    async def submit(self, query: str | None = None) -> None:
        """GO. Freezes the approved text and starts research in the background.

        *query* is what the user actually approved — it may differ from the ASR output,
        and that difference is the signal we keep (PLAN §7).
        """
        self._require("research.submit")
        approved = (query if query is not None else self.session.raw_transcript).strip()
        if not approved:
            raise CommandRejected(
                "refusing to research an empty query", user_message="Nothing to research."
            )
        self.session.submitted_query = approved
        self.session.error = None
        self._transition(SessionState.SUBMITTED)
        self._research_task = asyncio.create_task(self._research(approved))

    async def cancel(self) -> None:
        """Cancel outstanding research and return to the transcript (PLAN §22)."""
        task = self._research_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._research_task = None
        if self._state not in {SessionState.IDLE, SessionState.REVIEW}:
            self._transition(
                SessionState.REVIEW if self.session.raw_transcript else SessionState.IDLE
            )

    async def wait(self) -> None:
        """Await the in-flight research, if any. Used by the terminal prototype."""
        if self._research_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._research_task

    async def reset(self) -> None:
        await self.cancel()
        await self._engine.cancel_session()
        self.session = ResearchSession()
        self._emitter = EventEmitter(self._sink, session_id=self.session.id)
        self._transition(SessionState.IDLE)

    # -- internals -----------------------------------------------------------------
    async def _research(self, query: str) -> None:
        try:
            session, _result = await self._deps.research(
                query, self._settings, sink=self._sink, session=self.session
            )
            self.session = session
            self._state = SessionState.COMPLETED
        except asyncio.CancelledError:
            self._state = SessionState.CANCELLED
            self.session.status = SessionState.CANCELLED
            raise
        except VnrError as exc:
            # The query survives the failure so the user can retry it (PLAN §22).
            self._state = SessionState.FAILED
            self.session.status = SessionState.FAILED
            self.session.error = exc.user_message
        except Exception as exc:  # unexpected: still must not wedge the UI
            self._state = SessionState.FAILED
            self.session.status = SessionState.FAILED
            self.session.error = str(exc)
            self._emitter.research_failed("internal", "Something went wrong.")
            log(logger, logging.ERROR, "unexpected research failure", error=str(exc))
