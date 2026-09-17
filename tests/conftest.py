"""Shared fakes. Every test here runs offline, with no API keys."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from vnr.config import ResearchBudget, Settings
from vnr.events import EventEmitter, EventRecorder
from vnr.metrics import ResearchMetrics
from vnr.research.nebius import Completion, StreamDelta, ToolCall
from vnr.research.sources import SearchResult, SourceRegistry
from vnr.research.tavily import TavilyResponse
from vnr.research.tools import WebSearchTool


def result(n: int, *, host: str = "example.com", score: float = 0.9) -> SearchResult:
    return SearchResult(
        title=f"Result {n}",
        url=f"https://{host}/article-{n}",
        content=f"Body of result {n}.",
        score=score,
        published_date="2026-09-01",
    )


@dataclass
class FakeTavily:
    """Returns canned responses in order; records the parameters it was called with."""

    responses: list[TavilyResponse | Exception] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def search(self, query: str, **params: Any) -> TavilyResponse:
        self.calls.append({"query": query, **params})
        if not self.responses:
            return TavilyResponse(query=query, results=[result(1)])
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def aclose(self) -> None:
        return None


@dataclass
class FakeNebius:
    """Replays a scripted sequence of decision turns, then a scripted answer stream."""

    completions: list[Completion] = field(default_factory=list)
    answer_chunks: list[str] = field(default_factory=list)
    stream_usage: dict[str, Any] = field(default_factory=dict)
    tool_calls_seen: list[list[dict[str, Any]]] = field(default_factory=list)
    messages_seen: list[list[dict[str, Any]]] = field(default_factory=list)
    stream_options_seen: list[dict[str, Any]] = field(default_factory=list)

    async def create_tool_completion(
        self, messages: Sequence[dict[str, Any]], tools: Sequence[dict[str, Any]], **kwargs: Any
    ) -> Completion:
        self.messages_seen.append([dict(m) for m in messages])
        self.tool_calls_seen.append([dict(t) for t in tools])
        if not self.completions:
            return Completion(content="Evidence is sufficient.", finish_reason="stop")
        return self.completions.pop(0)

    async def stream_completion(
        self, messages: Sequence[dict[str, Any]], **kwargs: Any
    ) -> AsyncIterator[StreamDelta]:
        self.messages_seen.append([dict(m) for m in messages])
        self.stream_options_seen.append(dict(kwargs))
        for chunk in self.answer_chunks:
            yield StreamDelta(text=chunk)
        if self.stream_usage:
            yield StreamDelta(usage=self.stream_usage)

    async def aclose(self) -> None:
        return None


def tool_call(query: str, *, call_id: str = "call_1", **extra: Any) -> ToolCall:
    import json

    return ToolCall(
        id=call_id,
        name="web_search",
        raw_arguments=json.dumps({"query": query, **extra}),
    )


def search_completion(*calls: ToolCall) -> Completion:
    return Completion(content="", tool_calls=list(calls), finish_reason="tool_calls")


def make_tool(
    tavily: FakeTavily,
    *,
    budget: ResearchBudget | None = None,
    registry: SourceRegistry | None = None,
    recorder: EventRecorder | None = None,
    metrics: ResearchMetrics | None = None,
) -> tuple[WebSearchTool, SourceRegistry, EventRecorder, ResearchMetrics]:
    registry = registry or SourceRegistry()
    recorder = recorder or EventRecorder()
    metrics = metrics or ResearchMetrics()
    tool = WebSearchTool(
        tavily,  # type: ignore[arg-type]
        budget=budget or ResearchBudget(),
        registry=registry,
        emitter=EventEmitter(recorder, session_id="test"),
        metrics=metrics,
    )
    return tool, registry, recorder, metrics


def settings_without_keys(**budget_kwargs: Any) -> Settings:
    return Settings(budget=ResearchBudget(**budget_kwargs))


def make_agent(
    nebius: FakeNebius,
    tavily: FakeTavily,
    *,
    budget: ResearchBudget | None = None,
) -> tuple[Any, EventRecorder, SourceRegistry]:
    """Build a ResearchAgent whose tool and agent share one event recorder."""
    from datetime import date

    from vnr.research.agent import ResearchAgent

    recorder = EventRecorder()
    registry = SourceRegistry()
    metrics = ResearchMetrics()
    tool = WebSearchTool(
        tavily,  # type: ignore[arg-type]
        budget=budget or ResearchBudget(),
        registry=registry,
        emitter=EventEmitter(recorder, session_id="test"),
        metrics=metrics,
    )
    agent = ResearchAgent(
        nebius=nebius,  # type: ignore[arg-type]
        tool=tool,
        registry=registry,
        emitter=EventEmitter(recorder, session_id="test"),
        metrics=metrics,
        today=date(2026, 9, 17),
    )
    return agent, recorder, registry
