"""The wire contract between the Python service and the Swift UI.

The Swift checker (`macos/Sources/VNRKitCheck`) asserts against fixtures generated here,
so the two cannot drift apart unnoticed: change an event payload without regenerating,
and this test fails.
Regenerate with ``VNR_UPDATE_FIXTURES=1 uv run pytest tests/test_event_contract.py``.

This matters more than usual because agent sessions cannot compile Swift — the Python
suite is the only place the contract can be enforced automatically.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from vnr.events import EventEmitter, EventRecorder, SessionState

FIXTURES = Path(__file__).parent.parent / "macos/Fixtures/events.json"

#: Fixed so the golden file is stable across runs.
SESSION_ID = "fixture0001"


def sample_events() -> list[dict]:
    """One of every event the UI must handle, with realistic payloads."""
    recorder = EventRecorder()
    emitter = EventEmitter(recorder, session_id=SESSION_ID)

    emitter.state_changed(SessionState.LISTENING)
    emitter.asr_partial("find recent work on streaming")
    emitter.asr_final("find recent work on streaming ASR for Darija")
    emitter.research_started("find recent work on streaming ASR for Darija")
    emitter.search_started(
        index=1, query="streaming ASR low-resource languages", search_depth="advanced"
    )
    emitter.search_completed(
        index=1,
        query="streaming ASR low-resource languages",
        result_count=5,
        new_source_ids=["S1", "S2"],
        total_sources=5,
    )
    emitter.search_completed(index=2, query="failed one", result_count=0, error="tavily_rate_limit")
    emitter.synthesizing(source_count=5)
    emitter.reasoning_delta("Compare the retrieved evidence before answering.")
    emitter.answer_delta("Kyutai streams audio [1].")
    emitter.research_completed(
        stop_reason="evidence_sufficient",
        source_count=5,
        searches=2,
        turns=3,
        cited_sources=[
            {"number": 1, "id": "S1", "title": "Kyutai STT", "url": "https://kyutai.org/stt"}
        ],
        invalid_citation_ids=["S42"],
        metrics={"tavily_credits": 2, "go_to_completed_ms": 9720.4, "turns": 3},
    )
    emitter.research_failed("nebius_auth", "Nebius rejected the API key.")
    emitter.research_cancelled(query="cancelled query")

    events = [event.to_dict() for event in recorder.events]

    # asr.error is emitted by the service layer rather than the emitter helpers.
    events.append(
        {
            "type": "asr.error",
            "session_id": SESSION_ID,
            "ts": 0.0,
            "data": {"code": "microphone", "message": "The microphone delivered only silence."},
        }
    )
    # An event type this build of the UI does not know about: it must decode, not throw.
    events.append(
        {"type": "research.future_thing", "session_id": SESSION_ID, "ts": 0.0, "data": {}}
    )

    for event in events:
        event["ts"] = 0.0  # wall-clock would make the golden file unstable
    return events


def build_fixture() -> str:
    payload = [
        {"label": event["type"], "json": json.dumps(event, ensure_ascii=False, sort_keys=True)}
        for event in sample_events()
    ]
    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"


def test_the_swift_fixtures_match_what_the_service_emits():
    generated = build_fixture()
    if os.getenv("VNR_UPDATE_FIXTURES") == "1":
        FIXTURES.parent.mkdir(parents=True, exist_ok=True)
        FIXTURES.write_text(generated)
        pytest.skip("fixtures regenerated")

    assert FIXTURES.exists(), (
        f"{FIXTURES} is missing. Generate it with:\n"
        "  VNR_UPDATE_FIXTURES=1 uv run pytest tests/test_event_contract.py"
    )
    assert FIXTURES.read_text() == generated, (
        "The Swift test fixtures no longer match the events this service emits, so the "
        "macOS UI is being tested against a stale contract. Regenerate with:\n"
        "  VNR_UPDATE_FIXTURES=1 uv run pytest tests/test_event_contract.py"
    )


def test_every_event_type_the_ui_renders_is_covered():
    """A new event type must arrive with a fixture, or the Swift side never sees it."""
    from vnr.events import EventType

    covered = {event["type"] for event in sample_events()}
    missing = {e.value for e in EventType} - covered
    assert not missing, f"no fixture for: {sorted(missing)}"


# -- the other direction: commands the UI sends ------------------------------------
#
# `events.json` guards service → UI. Nothing guarded UI → service, which is the half the
# WebSocket client in slice 3 depends on: the service rejects an unknown command rather
# than ignoring it, so a mis-spelled string in Swift is a runtime rejection nobody sees
# until a Mac is in front of them.

#: Exactly the commands `_command()` dispatches. Mirrored by `ClientCommand` in
#: `macos/Sources/VNRKit/ClientCommand.swift`.
CLIENT_COMMANDS = {
    "recording.start",
    "recording.stop",
    "research.submit",
    "research.cancel",
    "session.reset",
}


def dispatched_commands() -> set[str]:
    """The command strings `_command` actually matches, read from its source."""
    import ast
    import inspect
    import textwrap

    from vnr import service

    tree = ast.parse(textwrap.dedent(inspect.getsource(service._command)))
    matched: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.match_case) and isinstance(node.pattern, ast.MatchValue):
            value = node.pattern.value
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                matched.add(value.value)
    return matched


def test_the_swift_client_and_the_service_agree_on_the_command_vocabulary():
    """Read from the dispatcher itself, so renaming a command there fails here."""
    assert dispatched_commands() == CLIENT_COMMANDS


def test_every_command_the_service_accepts_is_reachable_from_the_ui():
    """A command the service handles but no UI can send is dead weight; the reverse is a
    bug the user meets as a rejected click."""
    swift = (
        Path(__file__).parent.parent / "macos/Sources/VNRKit/ClientCommand.swift"
    ).read_text()
    for command in CLIENT_COMMANDS:
        assert f'"{command}"' in swift, f"{command} has no Swift counterpart"


async def test_health_reports_the_keys_that_gate_the_record_button():
    """`ServiceHealth` in Swift decodes exactly these; a rename would silently disable
    the record button rather than fail loudly."""
    from fastapi.testclient import TestClient

    from vnr.asr.mock import MockAsrEngine
    from vnr.config import Settings
    from vnr.service import create_app

    app = create_app(Settings(), engine=MockAsrEngine())
    with TestClient(app) as client:
        body = client.get("/health").json()

    assert set(body) == {"ready", "engine", "model", "error"}
    assert isinstance(body["ready"], bool)
    assert isinstance(body["engine"], str)
    assert isinstance(body["model"], str)
    assert body["error"] is None or isinstance(body["error"], str)


def test_the_busy_close_code_matches_the_swift_constant():
    """4409 is how the client tells 'another session holds the model' from 'it died'."""
    from vnr.service import BUSY_CODE

    swift = (
        Path(__file__).parent.parent / "macos/Sources/VNRKit/ServiceClient.swift"
    ).read_text()
    assert str(BUSY_CODE) in swift, f"BUSY_CODE {BUSY_CODE} is not in ServiceClient.swift"
