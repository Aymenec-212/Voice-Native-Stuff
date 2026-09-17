"""The lightweight research loop (docs/PLAN.md §9, §11, §17).

One model, one conversation, one tool, one bounded loop. No planner, no sub-agents, no
orchestration framework. The model decides whether another search is needed; the
application executes the tool and enforces every limit.

Turn structure:

    decision turns (not streamed, tools offered)
        └─ web_search → results appended as tool messages
    final synthesis (streamed, tools withheld)
        └─ answer deltas → citation rewriting → UI
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..errors import VnrError
from ..events import EventEmitter, SessionState
from ..logging import get_logger, log
from ..metrics import ResearchMetrics
from .citations import CitationReport, CitationRewriter, build_sources_section, tidy
from .nebius import MalformedToolCall, NebiusClient, ToolCall
from .prompts import synthesis_instruction, system_prompt
from .sources import SearchRecord, Source, SourceRegistry
from .tools import WEB_SEARCH_NAME, WEB_SEARCH_TOOL, ToolArgumentError, WebSearchTool

logger = get_logger("agent")


@dataclass
class ResearchResult:
    query: str
    answer: str = ""
    turns: int = 0
    sources: list[Source] = field(default_factory=list)
    cited: list[Source] = field(default_factory=list)
    searches: list[SearchRecord] = field(default_factory=list)
    citation_report: CitationReport = field(default_factory=CitationReport)
    metrics: ResearchMetrics = field(default_factory=ResearchMetrics)
    stop_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "answer": self.answer,
            "turns": self.turns,
            "stop_reason": self.stop_reason,
            "sources": [s.to_dict() for s in self.sources],
            "searches": [s.to_dict() for s in self.searches],
            "citations": self.citation_report.to_dict(),
            "metrics": self.metrics.to_dict(),
        }


class ResearchAgent:
    def __init__(
        self,
        *,
        nebius: NebiusClient,
        tool: WebSearchTool,
        registry: SourceRegistry,
        emitter: EventEmitter,
        metrics: ResearchMetrics,
        today: date | None = None,
    ) -> None:
        self._nebius = nebius
        self._tool = tool
        self._registry = registry
        self._emitter = emitter
        self._metrics = metrics
        self._today = today

    @property
    def budget(self):  # noqa: ANN201 - simple passthrough
        return self._tool.budget

    async def run(self, query: str) -> ResearchResult:
        """Research *query* and return a cited answer. Raises on unrecoverable failure."""
        result = ResearchResult(query=query, metrics=self._metrics)
        self._emitter.state_changed(SessionState.RESEARCH_STARTED)
        self._emitter.research_started(query)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt(self.budget, today=self._today)},
            {"role": "user", "content": query},
        ]

        try:
            stop_reason = await self._decision_rounds(messages, result)
            await self._synthesize(query, messages, result)
        except asyncio.CancelledError:
            self._emitter.state_changed(SessionState.CANCELLED)
            self._emitter.research_cancelled(query=query)
            raise
        except VnrError as exc:
            self._emitter.state_changed(SessionState.FAILED)
            self._emitter.research_failed(exc.code, exc.user_message)
            log(logger, logging.ERROR, "research failed", code=exc.code, error=str(exc))
            raise

        result.stop_reason = stop_reason
        result.sources = self._registry.all()
        result.searches = self._tool.records
        self._metrics.mark(ResearchMetrics.COMPLETED)
        self._emitter.state_changed(SessionState.COMPLETED)
        self._emitter.research_completed(
            stop_reason=stop_reason,
            source_count=len(result.sources),
            searches=self._tool.state.searches_used,
            turns=result.turns,
            # Numbered exactly as the answer's [n] markers, so citations can be clickable.
            cited_sources=[
                {"number": n, "id": s.id, "title": s.title, "url": s.url}
                for n, s in enumerate(result.cited, start=1)
            ],
            invalid_citation_ids=result.citation_report.invalid_ids,
            metrics=self._metrics.to_dict(),
        )
        log(
            logger,
            logging.INFO,
            "research completed",
            turns=result.turns,
            searches=self._tool.state.searches_used,
            sources=len(result.sources),
            stop_reason=stop_reason,
            metrics=self._metrics.to_dict(),
        )
        return result

    # -- phase 1: bounded decision/tool rounds ---------------------------------------
    async def _decision_rounds(
        self, messages: list[dict[str, Any]], result: ResearchResult
    ) -> str:
        for turn in range(1, self.budget.max_turns + 1):
            result.turns = turn
            self._metrics.turns = turn

            if self._tool.state.exhausted and turn > 1:
                return "search_budget_exhausted"

            completion = await self._nebius.create_tool_completion(
                messages,
                tools=[WEB_SEARCH_TOOL],
                max_tokens=self.budget.decision_max_tokens,
            )
            self._metrics.mark(ResearchMetrics.FIRST_MODEL_RESPONSE)
            self._metrics.record_usage(completion.usage)
            messages.append(completion.to_assistant_message())

            if not completion.wants_tools:
                return "evidence_sufficient"

            for call in completion.tool_calls:
                messages.append(await self._execute(call))

        return "turn_limit_reached"

    async def _execute(self, call: ToolCall) -> dict[str, Any]:
        """Run one tool call, converting every failure into a message the model can use."""
        content: str
        if call.name != WEB_SEARCH_NAME:
            content = (
                f"Unknown tool {call.name!r}. The only available tool is {WEB_SEARCH_NAME}."
            )
            log(logger, logging.WARNING, "unknown tool requested", name=call.name)
        else:
            try:
                content = await self._tool.run(call.arguments())
            except (MalformedToolCall, ToolArgumentError) as exc:
                content = f"That {WEB_SEARCH_NAME} call could not be executed: {exc}. Retry it."
                log(logger, logging.WARNING, "malformed tool call", error=str(exc))
        return {
            "role": "tool",
            "tool_call_id": call.id,
            "name": call.name,
            "content": content,
        }

    # -- phase 2: streamed final synthesis -------------------------------------------
    async def _synthesize(
        self, query: str, messages: list[dict[str, Any]], result: ResearchResult
    ) -> None:
        self._metrics.mark(ResearchMetrics.SYNTHESIS_STARTED)
        self._emitter.state_changed(SessionState.SYNTHESIZING)
        self._emitter.synthesizing(source_count=len(self._registry))

        messages.append({"role": "user", "content": synthesis_instruction(query, self._registry)})
        rewriter = CitationRewriter(self._registry)
        parts: list[str] = []
        streaming_state_sent = False

        async for delta in self._nebius.stream_completion(
            messages, max_tokens=self.budget.answer_max_tokens, tool_choice="none"
        ):
            self._metrics.record_usage(delta.usage)
            if not delta.text:
                continue
            if not streaming_state_sent:
                self._metrics.mark(ResearchMetrics.FIRST_ANSWER_TOKEN)
                self._emitter.state_changed(SessionState.ANSWER_STREAMING)
                streaming_state_sent = True
            visible = rewriter.feed(delta.text)
            if visible:
                parts.append(visible)
                self._emitter.answer_delta(visible)

        tail = rewriter.flush()
        if tail:
            parts.append(tail)
            self._emitter.answer_delta(tail)

        body = tidy("".join(parts))
        section = build_sources_section(rewriter.report, self._registry)
        if section:
            # Streamed too, so a delta-only consumer (the CLI) shows the same text the
            # session object stores. The UI gets the structured list on research.completed.
            self._emitter.answer_delta("\n\n" + section)
        result.answer = f"{body}\n\n{section}".strip() if section else body
        result.cited = rewriter.report.cited
        result.citation_report = rewriter.report
        if rewriter.report.invalid_ids:
            log(
                logger,
                logging.WARNING,
                "dropped invalid citations",
                ids=rewriter.report.invalid_ids,
            )
