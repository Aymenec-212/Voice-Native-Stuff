"""The state machine that guarantees research never starts without approval."""

import asyncio

import pytest

from vnr.asr.mock import MockAsrEngine
from vnr.config import Settings
from vnr.controller import CommandRejected, ControllerDeps, SessionController
from vnr.errors import AsrUnavailableError, NebiusAuthError
from vnr.events import EventRecorder, EventType, SessionState
from vnr.research.agent import ResearchResult
from vnr.session import ResearchSession


class SpyResearch:
    """Stands in for run_research; records what the controller actually sent."""

    def __init__(self, *, answer: str = "An answer.", error: Exception | None = None) -> None:
        self.answer = answer
        self.error = error
        self.queries: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    async def __call__(self, query, settings, *, sink=None, session=None, **kwargs):
        self.queries.append(query)
        self.started.set()
        await self.release.wait()
        if self.error:
            raise self.error
        session = session or ResearchSession()
        session.answer = self.answer
        return session, ResearchResult(query=query, answer=self.answer)


async def make_controller(*, research: SpyResearch | None = None, loaded: bool = True):
    engine = MockAsrEngine(frames_per_word=1)
    if loaded:
        await engine.load()
    recorder = EventRecorder()
    spy = research or SpyResearch()
    controller = SessionController(
        engine, Settings(), sink=recorder, deps=ControllerDeps(research=spy)
    )
    return controller, recorder, spy


async def speak(controller: SessionController, frames: int = 4) -> str:
    await controller.start_recording()
    for _ in range(frames):
        await controller.push_audio(b"\x00" * 3840)
    return await controller.stop_recording()


# -- the central guarantee ---------------------------------------------------------
async def test_recording_end_lands_in_review_and_starts_nothing():
    """PLAN §2.2/§7: the streaming path terminates at the transcript editor."""
    controller, _, spy = await make_controller()

    transcript = await speak(controller)

    assert controller.state is SessionState.REVIEW
    assert transcript
    assert spy.queries == [], "research must not start without an explicit GO"


async def test_go_sends_the_edited_text_not_the_asr_output():
    controller, _, spy = await make_controller()
    await speak(controller)
    raw = controller.raw_transcript

    await controller.submit("find recent work on streaming ASR for Darija")
    await controller.wait()

    assert spy.queries == ["find recent work on streaming ASR for Darija"]
    assert controller.session.raw_transcript == raw
    assert controller.session.transcript_was_edited is True
    assert controller.state is SessionState.COMPLETED


async def test_go_defaults_to_the_transcript_when_nothing_was_edited():
    controller, _, spy = await make_controller()
    transcript = await speak(controller)
    await controller.submit()
    await controller.wait()
    assert spy.queries == [transcript]
    assert controller.session.transcript_was_edited is False


# -- refusals ----------------------------------------------------------------------
async def test_recording_is_refused_until_the_model_is_resident():
    controller, _, _ = await make_controller(loaded=False)
    with pytest.raises(AsrUnavailableError):
        await controller.start_recording()
    assert controller.state is SessionState.IDLE


@pytest.mark.parametrize(
    ("command", "state"),
    [("submit", SessionState.IDLE), ("push_audio", SessionState.IDLE)],
)
async def test_commands_are_refused_in_the_wrong_state(command, state):
    controller, _, spy = await make_controller()
    assert controller.state is state
    with pytest.raises(CommandRejected):
        await (controller.submit() if command == "submit" else controller.push_audio(b"\x00"))
    assert spy.queries == []


async def test_an_empty_query_is_refused():
    controller, _, spy = await make_controller()
    await speak(controller)
    with pytest.raises(CommandRejected):
        await controller.submit("   ")
    assert spy.queries == []
    assert controller.state is SessionState.REVIEW


async def test_a_second_recording_cannot_start_mid_utterance():
    controller, _, _ = await make_controller()
    await controller.start_recording()
    with pytest.raises(CommandRejected):
        await controller.start_recording()


# -- cancellation and failure ------------------------------------------------------
async def test_cancel_returns_to_the_transcript():
    spy = SpyResearch()
    spy.release.clear()
    controller, recorder, _ = await make_controller(research=spy)
    await speak(controller)
    await controller.submit()
    await spy.started.wait()

    await controller.cancel()

    assert controller.state is SessionState.REVIEW, "the user gets their transcript back"
    assert controller.raw_transcript


async def test_a_failed_run_keeps_the_query_for_a_retry():
    """PLAN §22: a provider failure must not lose what the user approved."""
    controller, _, _ = await make_controller(research=SpyResearch(error=NebiusAuthError()))
    await speak(controller)
    await controller.submit("what is kyutai stt?")
    await controller.wait()

    assert controller.state is SessionState.FAILED
    assert controller.session.submitted_query == "what is kyutai stt?"
    assert controller.session.error == "Nebius rejected the API key."


async def test_an_unexpected_failure_still_reports_to_the_ui():
    controller, recorder, _ = await make_controller(
        research=SpyResearch(error=RuntimeError("boom"))
    )
    await speak(controller)
    await controller.submit()
    await controller.wait()
    assert controller.state is SessionState.FAILED
    assert recorder.of_type(EventType.RESEARCH_FAILED)[0].data["code"] == "internal"


# -- events ------------------------------------------------------------------------
async def test_the_ui_can_follow_the_whole_state_sequence():
    controller, recorder, _ = await make_controller()
    await speak(controller)
    await controller.submit()
    await controller.wait()

    states = [e.data["state"] for e in recorder.of_type(EventType.STATE_CHANGED)]
    assert states[:4] == ["LISTENING", "FINALIZING_TRANSCRIPT", "REVIEW", "SUBMITTED"]
    assert recorder.of_type(EventType.ASR_PARTIAL)
    assert recorder.of_type(EventType.ASR_FINAL)


async def test_each_utterance_gets_a_fresh_session():
    controller, _, _ = await make_controller()
    await speak(controller)
    first = controller.session.id
    await controller.submit()
    await controller.wait()

    await speak(controller)
    assert controller.session.id != first
    assert controller.session.answer == ""


async def test_reset_returns_to_idle():
    controller, _, _ = await make_controller()
    await speak(controller)
    await controller.reset()
    assert controller.state is SessionState.IDLE
    assert controller.raw_transcript == ""


async def test_you_can_speak_again_after_an_answer():
    controller, _, _ = await make_controller()
    await speak(controller)
    await controller.submit()
    await controller.wait()
    assert controller.state is SessionState.COMPLETED

    await controller.start_recording()  # must not be refused
    assert controller.state is SessionState.LISTENING


async def test_a_failure_can_be_retried_without_re_recording():
    """PLAN §22: keep the approved transcript visible so the user can retry."""
    spy = SpyResearch(error=NebiusAuthError())
    controller, _, _ = await make_controller(research=spy)
    await speak(controller)
    await controller.submit("what is kyutai stt?")
    await controller.wait()
    assert controller.state is SessionState.FAILED

    spy.error = None
    await controller.submit("what is kyutai stt?")
    await controller.wait()

    assert controller.state is SessionState.COMPLETED
    assert controller.session.error is None
    assert spy.queries == ["what is kyutai stt?", "what is kyutai stt?"]


async def test_recording_is_refused_while_research_is_running():
    spy = SpyResearch()
    spy.release.clear()
    controller, _, _ = await make_controller(research=spy)
    await speak(controller)
    await controller.submit()
    await spy.started.wait()

    with pytest.raises(CommandRejected):
        await controller.start_recording()

    spy.release.set()
    await controller.wait()
