"""The loopback WebSocket protocol the SwiftUI app will speak (docs/PLAN.md §19)."""

import json

import pytest
from fastapi.testclient import TestClient

from vnr.asr.mock import MockAsrEngine
from vnr.config import ResearchBudget, Settings
from vnr.controller import ControllerDeps
from vnr.errors import NebiusAuthError
from vnr.events import EventType
from vnr.research.agent import ResearchResult
from vnr.service import BUSY_CODE, create_app
from vnr.session import ResearchSession

FRAME = b"\x00" * 3840


def make_client(*, answer: str = "Kyutai streams audio [1].", error=None) -> TestClient:
    seen: list[str] = []

    async def research(query, settings, *, sink=None, session=None, **kwargs):
        seen.append(query)
        if error:
            raise error
        session = session or ResearchSession()
        if sink is not None:
            from vnr.events import EventEmitter

            emitter = EventEmitter(sink, session_id=session.id)
            emitter.research_started(query)
            emitter.search_started(1, "kyutai stt")
            emitter.search_completed(1, "kyutai stt", 3)
            emitter.synthesizing(3)
            emitter.answer_delta(answer)
            emitter.research_completed(turns=2, searches=1, source_count=3)
        session.answer = answer
        return session, ResearchResult(query=query, answer=answer)

    app = create_app(
        Settings(budget=ResearchBudget()),
        engine=MockAsrEngine(frames_per_word=1),
        deps=ControllerDeps(research=research),
    )
    client = TestClient(app)
    client.queries = seen  # type: ignore[attr-defined]
    return client


def drain(ws, until: str, limit: int = 60) -> list[dict]:
    """Collect events up to and including the first one of type *until*."""
    return _drain(ws, lambda e: e["type"] == until, f"type {until}", limit)


def drain_state(ws, state: str, limit: int = 60) -> list[dict]:
    """Collect events up to and including the transition into *state*."""
    return _drain(
        ws,
        lambda e: e["type"] == "session.state_changed" and e["data"]["state"] == state,
        f"state {state}",
        limit,
    )


def _drain(ws, matches, description: str, limit: int) -> list[dict]:
    events = []
    for _ in range(limit):
        event = json.loads(ws.receive_text())
        events.append(event)
        if matches(event):
            return events
    raise AssertionError(f"never saw {description}; got {[e['type'] for e in events]}")


def test_health_reports_runtime_readiness():
    with make_client() as client:
        body = client.get("/health").json()
    assert body["ready"] is True
    assert body["engine"] == "mock"
    assert body["error"] is None


def test_the_full_path_from_audio_to_a_streamed_answer():
    with make_client() as client, client.websocket_connect("/ws") as ws:
        opening = json.loads(ws.receive_text())
        assert opening["data"]["state"] == "IDLE"
        assert opening["data"]["ready"] is True

        ws.send_text(json.dumps({"type": "recording.start"}))
        for _ in range(4):
            ws.send_bytes(FRAME)
        ws.send_text(json.dumps({"type": "recording.stop"}))

        events = drain_state(ws, "REVIEW")
        types = [e["type"] for e in events]
        assert EventType.ASR_PARTIAL.value in types
        finals = [e for e in events if e["type"] == EventType.ASR_FINAL.value]
        assert finals and finals[0]["data"]["text"]

        # The transcript is final and the socket is parked — nothing was researched.
        assert client.queries == []
        states = [e["data"]["state"] for e in events if e["type"] == "session.state_changed"]
        assert states == ["LISTENING", "FINALIZING_TRANSCRIPT", "REVIEW"]

        ws.send_text(json.dumps({"type": "research.submit", "query": "edited query"}))
        events = drain(ws, EventType.RESEARCH_COMPLETED.value)
        types = [e["type"] for e in events]

    assert client.queries == ["edited query"], "the service must send the approved text"
    assert types.count(EventType.RESEARCH_SEARCH_STARTED.value) == 1
    assert EventType.RESEARCH_ANSWER_DELTA.value in types


def test_answer_deltas_arrive_without_the_client_saying_anything():
    """Research runs in the background; its events must not wait for the next command."""
    with make_client(answer="streamed") as client, client.websocket_connect("/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "recording.start"}))
        ws.send_bytes(FRAME)
        ws.send_text(json.dumps({"type": "recording.stop"}))
        drain_state(ws, "REVIEW")

        ws.send_text(json.dumps({"type": "research.submit"}))
        # One send, then only receives — no further client traffic to pump the queue.
        events = drain(ws, EventType.RESEARCH_COMPLETED.value)

    deltas = [e for e in events if e["type"] == EventType.RESEARCH_ANSWER_DELTA.value]
    assert "".join(d["data"]["text"] for d in deltas) == "streamed"


def test_submitting_before_review_is_refused_and_the_socket_survives():
    with make_client() as client, client.websocket_connect("/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "research.submit", "query": "sneaky"}))
        error = json.loads(ws.receive_text())
        assert error["type"] == EventType.ASR_ERROR.value
        assert error["data"]["code"] == "rejected"

        # still usable afterwards
        ws.send_text(json.dumps({"type": "recording.start"}))
        assert json.loads(ws.receive_text())["data"]["state"] == "LISTENING"
    assert client.queries == []


def test_audio_outside_a_recording_is_refused():
    with make_client() as client, client.websocket_connect("/ws") as ws:
        ws.receive_text()
        ws.send_bytes(FRAME)
        assert json.loads(ws.receive_text())["data"]["code"] == "rejected"


@pytest.mark.parametrize("payload", ["not json", '["a list"]', '{"type": "shell.exec"}'])
def test_malformed_and_unknown_commands_are_rejected(payload):
    with make_client() as client, client.websocket_connect("/ws") as ws:
        ws.receive_text()
        ws.send_text(payload)
        assert json.loads(ws.receive_text())["data"]["code"] == "rejected"


def test_a_provider_failure_reaches_the_ui():
    with make_client(error=NebiusAuthError()) as client, client.websocket_connect("/ws") as ws:
        ws.receive_text()
        ws.send_text(json.dumps({"type": "recording.start"}))
        ws.send_bytes(FRAME)
        ws.send_text(json.dumps({"type": "recording.stop"}))
        drain_state(ws, "REVIEW")
        ws.send_text(json.dumps({"type": "research.submit"}))
        ws.send_text(json.dumps({"type": "session.reset"}))
        events = drain(ws, "session.state_changed", limit=20)
    assert any(e["type"] == "session.state_changed" for e in events)


def test_only_one_session_at_a_time():
    with make_client() as client, client.websocket_connect("/ws") as first:
        first.receive_text()
        from starlette.websockets import WebSocketDisconnect

        with pytest.raises(WebSocketDisconnect) as exc, client.websocket_connect("/ws") as second:
            second.receive_text()
        assert exc.value.code == BUSY_CODE


def test_the_service_starts_even_when_the_asr_runtime_is_broken():
    """PLAN §22: report not-ready rather than refusing to run at all."""
    from vnr.config import AsrConfig
    from vnr.errors import AsrUnavailableError

    class Broken(MockAsrEngine):
        async def load(self):
            raise AsrUnavailableError("no binary")

    app = create_app(Settings(asr=AsrConfig(engine="mock")), engine=Broken())
    with TestClient(app) as client:
        body = client.get("/health").json()
        assert body["ready"] is False
        assert body["error"] == "Speech recognition is not ready."

        with client.websocket_connect("/ws") as ws:
            assert json.loads(ws.receive_text())["data"]["ready"] is False
            ws.send_text(json.dumps({"type": "recording.start"}))
            assert json.loads(ws.receive_text())["data"]["code"] == "asr_unavailable"


def test_the_exact_json_the_swift_client_emits_drives_a_whole_session():
    """Not a re-encoding — the literal strings `ClientCommand.jsonText()` produces.

    `JSONSerialization` with `.sortedKeys` writes no spaces and orders keys
    alphabetically, so `research.submit` goes out as `{"query":…,"type":…}` with the type
    *second*. Anything on the Python side that read the first key, or split on `", "`,
    would pass every other test here and fail on a Mac.
    """
    start = '{"type":"recording.start"}'
    stop = '{"type":"recording.stop"}'
    submit = '{"query":"compare Kyutai and Nebius","type":"research.submit"}'

    with make_client() as client, client.websocket_connect("/ws") as ws:
        ws.receive_text()
        ws.send_text(start)
        for _ in range(4):
            ws.send_bytes(FRAME)
        ws.send_text(stop)
        drain_state(ws, "REVIEW")

        ws.send_text(submit)
        events = drain(ws, EventType.RESEARCH_COMPLETED.value)

    assert client.queries == ["compare Kyutai and Nebius"]
    assert EventType.RESEARCH_ANSWER_DELTA.value in [e["type"] for e in events]


def test_a_second_connection_is_refused_with_the_code_the_client_checks():
    """One user, one resident model. The client distinguishes this from a dead service
    by the close code alone, so the code is part of the contract."""
    from starlette.websockets import WebSocketDisconnect

    with make_client() as client, client.websocket_connect("/ws"):
        with (
            pytest.raises(WebSocketDisconnect) as exc,
            client.websocket_connect("/ws") as second,
        ):
            second.receive_text()
        assert exc.value.code == BUSY_CODE


def test_the_service_streams_the_answer_and_nothing_else():
    """The macOS client accumulates deltas into what it draws, and renders sources from
    the structured list beside it. If the service also streams a text Sources block, a
    client using both draws the same list twice — which it did."""
    with make_client() as client, client.websocket_connect("/ws") as ws:
        ws.receive_text()
        ws.send_text('{"type":"recording.start"}')
        ws.send_bytes(FRAME)
        ws.send_text('{"type":"recording.stop"}')
        drain_state(ws, "REVIEW")

        ws.send_text('{"query":"q","type":"research.submit"}')
        events = drain(ws, EventType.RESEARCH_COMPLETED.value)

    streamed = "".join(
        e["data"]["text"]
        for e in events
        if e["type"] == EventType.RESEARCH_ANSWER_DELTA.value
    )
    assert "Sources" not in streamed, "sources belong in cited_sources, not the answer"
    assert "http" not in streamed, "the model writes [n] markers; URLs come from Tavily"
    assert streamed, "the answer itself still streams"
    # That `cited_sources` travels on research.completed is asserted against the real
    # agent in test_agent.py; this client's research is a stub with no sources.
