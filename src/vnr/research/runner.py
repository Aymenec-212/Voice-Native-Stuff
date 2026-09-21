"""Wiring: build the clients, the tool and the agent, run one query, clean up.

Keeps the CLI (and later the WebSocket service) free of construction details.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx

from ..config import Settings
from ..events import EventEmitter, EventSink, null_sink
from ..metrics import ResearchMetrics
from ..session import ResearchSession
from .agent import ResearchAgent, ResearchResult
from .nebius import NebiusClient
from .sources import SourceRegistry
from .tavily import TavilyClient
from .tools import WebSearchTool


@dataclass
class ResearchRuntime:
    agent: ResearchAgent
    registry: SourceRegistry
    metrics: ResearchMetrics
    nebius: NebiusClient
    tool: WebSearchTool


@asynccontextmanager
async def research_runtime(
    settings: Settings,
    *,
    sink: EventSink = null_sink,
    session_id: str = "",
    http_client: httpx.AsyncClient | None = None,
) -> AsyncIterator[ResearchRuntime]:
    """Construct a ready-to-run agent. Credentials are checked lazily, at first call."""
    nebius = NebiusClient(settings.nebius, client=http_client)
    tavily = TavilyClient(settings.tavily, client=http_client)
    registry = SourceRegistry()
    metrics = ResearchMetrics()
    emitter = EventEmitter(sink, session_id=session_id)
    tool = WebSearchTool(
        tavily,
        budget=settings.budget,
        registry=registry,
        emitter=emitter,
        metrics=metrics,
    )
    agent = ResearchAgent(
        nebius=nebius, tool=tool, registry=registry, emitter=emitter, metrics=metrics
    )
    try:
        yield ResearchRuntime(
            agent=agent, registry=registry, metrics=metrics, nebius=nebius, tool=tool
        )
    finally:
        await nebius.aclose()
        await tavily.aclose()


async def run_research(
    query: str,
    settings: Settings,
    *,
    sink: EventSink = null_sink,
    session: ResearchSession | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> tuple[ResearchSession, ResearchResult]:
    """Run one approved query and fold the outcome into a :class:`ResearchSession`."""
    from ..events import SessionState

    session = session or ResearchSession()
    session.submitted_query = query
    session.status = SessionState.SUBMITTED

    async with research_runtime(
        settings, sink=sink, session_id=session.id, http_client=http_client
    ) as runtime:
        session.research_metrics = runtime.metrics
        try:
            result = await runtime.agent.run(query)
        except Exception as exc:
            session.status = SessionState.FAILED
            session.error = str(exc)
            session.searches = runtime.tool.records
            session.sources = runtime.registry.all()
            raise
        session.status = SessionState.COMPLETED
        session.answer = result.answer
        session.reasoning = result.reasoning
        session.searches = result.searches
        session.sources = result.sources
        return session, result
