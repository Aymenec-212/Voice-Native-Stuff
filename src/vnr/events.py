"""The vocabulary the UI consumes (docs/PLAN.md §4, §18, §19).

The UI must be able to render the whole experience from these events alone, without
knowing anything about Nebius, Tavily or the ASR runtime. Payloads describe *actions and
states*. Provider reasoning travels in its own optional disclosure stream, never in
answer deltas.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class SessionState(StrEnum):
    """States a session moves through (PLAN §18)."""

    IDLE = "IDLE"
    LISTENING = "LISTENING"
    FINALIZING_TRANSCRIPT = "FINALIZING_TRANSCRIPT"
    REVIEW = "REVIEW"
    SUBMITTED = "SUBMITTED"
    RESEARCH_STARTED = "RESEARCH_STARTED"
    SEARCH_STARTED = "SEARCH_STARTED"
    SEARCH_COMPLETED = "SEARCH_COMPLETED"
    SYNTHESIZING = "SYNTHESIZING"
    ANSWER_STREAMING = "ANSWER_STREAMING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class EventType(StrEnum):
    """Wire names for service → UI events (PLAN §19)."""

    ASR_PARTIAL = "asr.partial"
    ASR_FINAL = "asr.final"
    ASR_ERROR = "asr.error"

    RESEARCH_STARTED = "research.started"
    RESEARCH_SEARCH_STARTED = "research.search_started"
    RESEARCH_SEARCH_COMPLETED = "research.search_completed"
    RESEARCH_SYNTHESIZING = "research.synthesizing"
    RESEARCH_REASONING_DELTA = "research.reasoning_delta"
    RESEARCH_ANSWER_DELTA = "research.answer_delta"
    RESEARCH_COMPLETED = "research.completed"
    RESEARCH_FAILED = "research.failed"
    RESEARCH_CANCELLED = "research.cancelled"

    STATE_CHANGED = "session.state_changed"


@dataclass(frozen=True)
class Event:
    type: EventType
    data: dict[str, Any] = field(default_factory=dict)
    session_id: str = ""
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "session_id": self.session_id,
            "ts": round(self.ts, 6),
            "data": self.data,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)


#: Sinks are synchronous and must not block — a WebSocket sink pushes onto a queue.
EventSink = Callable[[Event], None]


def null_sink(event: Event) -> None:  # noqa: ARG001 - deliberately does nothing
    """Drop events. Useful when a caller only wants the return value."""


class EventRecorder:
    """Collects events in order. Used by the CLI's transcript log and by tests."""

    def __init__(self) -> None:
        self.events: list[Event] = []

    def __call__(self, event: Event) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type.value for e in self.events]

    def of_type(self, event_type: EventType) -> list[Event]:
        return [e for e in self.events if e.type is event_type]

    def answer_text(self) -> str:
        return "".join(
            str(e.data.get("text", "")) for e in self.of_type(EventType.RESEARCH_ANSWER_DELTA)
        )


class EventEmitter:
    """Binds a sink to a session id so call sites stay short."""

    def __init__(self, sink: EventSink = null_sink, session_id: str = "") -> None:
        self._sink = sink
        self.session_id = session_id

    def emit(self, event_type: EventType, /, **data: Any) -> Event:
        event = Event(type=event_type, data=data, session_id=self.session_id)
        self._sink(event)
        return event

    # --- ASR ------------------------------------------------------------------
    def asr_partial(self, text: str, **extra: Any) -> Event:
        return self.emit(EventType.ASR_PARTIAL, text=text, **extra)

    def asr_final(self, text: str, **extra: Any) -> Event:
        return self.emit(EventType.ASR_FINAL, text=text, **extra)

    # --- Research -------------------------------------------------------------
    def research_started(self, query: str, **extra: Any) -> Event:
        return self.emit(EventType.RESEARCH_STARTED, query=query, **extra)

    def search_started(self, index: int, query: str, **params: Any) -> Event:
        return self.emit(EventType.RESEARCH_SEARCH_STARTED, index=index, query=query, **params)

    def search_completed(self, index: int, query: str, result_count: int, **extra: Any) -> Event:
        return self.emit(
            EventType.RESEARCH_SEARCH_COMPLETED,
            index=index,
            query=query,
            result_count=result_count,
            **extra,
        )

    def synthesizing(self, source_count: int, **extra: Any) -> Event:
        return self.emit(EventType.RESEARCH_SYNTHESIZING, source_count=source_count, **extra)

    def reasoning_delta(self, text: str) -> Event:
        return self.emit(EventType.RESEARCH_REASONING_DELTA, text=text)

    def answer_delta(self, text: str) -> Event:
        return self.emit(EventType.RESEARCH_ANSWER_DELTA, text=text)

    def research_completed(self, **data: Any) -> Event:
        return self.emit(EventType.RESEARCH_COMPLETED, **data)

    def research_failed(self, code: str, message: str, **extra: Any) -> Event:
        return self.emit(EventType.RESEARCH_FAILED, code=code, message=message, **extra)

    def research_cancelled(self, **extra: Any) -> Event:
        return self.emit(EventType.RESEARCH_CANCELLED, **extra)

    def state_changed(self, state: SessionState, **extra: Any) -> Event:
        return self.emit(EventType.STATE_CHANGED, state=state.value, **extra)
