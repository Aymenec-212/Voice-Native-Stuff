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
