"""The one external tool V1 exposes: ``web_search`` (docs/PLAN.md §10, §11, §14).

The model sees a narrow, application-owned wrapper — not Tavily's API surface. The
application keeps control of result counts, answer/image/raw-content flags, the search
budget and the depth policy, and may clamp anything the model asks for.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from ..config import CREDITS_PER_DEPTH, ResearchBudget
from ..errors import TavilyError
from ..events import EventEmitter
from ..logging import get_logger, log
from ..metrics import ResearchMetrics
from .sources import SearchRecord, Source, SourceRegistry
from .tavily import DEPTHS, TIME_RANGES, TOPICS, TavilyClient

logger = get_logger("tools")

WEB_SEARCH_NAME = "web_search"

WEB_SEARCH_TOOL: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": WEB_SEARCH_NAME,
        "description": (
            "Search the live web for evidence. Use a concise, focused query — break a "
            "complex question into several separate searches rather than one long one. "
            "Returns numbered sources you must cite as [S1], [S2], …"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Concise search query, ideally under 12 words.",
                },
                "topic": {
                    "type": "string",
                    "enum": list(TOPICS),
                    "description": "Use 'news' for current events, 'finance' for markets.",
                },
                "time_range": {
                    "type": "string",
                    "enum": list(TIME_RANGES),
                    "description": (
                        "Restrict to recent results when the question is time-sensitive."
                    ),
                },
                "search_depth": {
                    "type": "string",
                    "enum": list(DEPTHS),
                    "description": (
                        "'basic' by default. Request 'advanced' only for deep research or "
                        "when basic results were too weak; it may be denied."
                    ),
                },
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    },
}


class ToolArgumentError(Exception):
    """The model's arguments could not be used (reported back to the model)."""


@dataclass
class SearchBudgetState:
    """Live budget counters for one research session (PLAN §11)."""

    budget: ResearchBudget
    searches_used: int = 0
    advanced_used: int = 0

    @property
    def searches_remaining(self) -> int:
        return max(0, self.budget.max_searches - self.searches_used)

    @property
    def exhausted(self) -> bool:
        return self.searches_remaining == 0

    def resolve_depth(self, requested: str | None) -> tuple[str, str | None]:
        """Apply the depth policy. Returns ``(depth, reason_if_clamped)`` (PLAN §14)."""
        wanted = (requested or self.budget.default_depth).lower()
        if wanted == "fast":
            wanted = "basic"
        if wanted not in DEPTHS:
            return self.budget.default_depth, f"unknown depth {requested!r}"
        if wanted != "advanced":
            return wanted, None
        if not self.budget.allow_advanced:
            return self.budget.default_depth, "advanced searches are disabled"
        if self.advanced_used >= self.budget.max_advanced_searches:
            return self.budget.default_depth, "advanced search budget spent"
        return "advanced", None

    def spend(self, depth: str) -> int:
        self.searches_used += 1
        if depth == "advanced":
            self.advanced_used += 1
        return CREDITS_PER_DEPTH.get(depth, 1)


def validate_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    """Validate and clamp the model's arguments. Raises on anything unusable."""
    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolArgumentError("'query' is required and must be a non-empty string")
    cleaned: dict[str, Any] = {"query": " ".join(query.split())[:400]}

    topic = arguments.get("topic")
    cleaned["topic"] = topic if topic in TOPICS else "general"

    time_range = arguments.get("time_range")
    cleaned["time_range"] = time_range if time_range in TIME_RANGES else None

    depth = arguments.get("search_depth")
    cleaned["search_depth"] = depth if isinstance(depth, str) else None
    return cleaned


class WebSearchTool:
    """Executes ``web_search`` calls, enforces budgets, records events and metrics."""

    def __init__(
        self,
        client: TavilyClient,
        *,
        budget: ResearchBudget,
        registry: SourceRegistry,
        emitter: EventEmitter,
        metrics: ResearchMetrics,
        clock: Any = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._emitter = emitter
        self._metrics = metrics
        self._clock = clock or time.monotonic
        self.state = SearchBudgetState(budget=budget)
        self.records: list[SearchRecord] = []
        self._seen: dict[tuple[str, str, str, str | None], SearchRecord] = {}

    @property
    def budget(self) -> ResearchBudget:
        return self.state.budget

    async def run(self, arguments: dict[str, Any]) -> str:
        """Execute one search and return the tool message content for the model."""
        args = validate_arguments(arguments)
        if self.state.exhausted:
            return (
                "Search budget exhausted — no searches remain. Answer now using the "
                "sources already retrieved, and say plainly if the evidence is insufficient."
            )

        depth, clamp_reason = self.state.resolve_depth(args["search_depth"])
        key = (args["query"].lower(), args["topic"], depth, args["time_range"])
        if key in self._seen:
            previous = self._seen[key]
            return (
                f"That exact search was already run (search #{previous.index}) and returned "
                f"{', '.join(previous.new_source_ids) or 'no new sources'}. "
                "Search something different or answer with what you have."
            )

        index = len(self.records) + 1
        record = SearchRecord(
            index=index,
            query=args["query"],
            parameters={
                "topic": args["topic"],
                "search_depth": depth,
                "time_range": args["time_range"],
                "max_results": self.budget.max_results_per_search,
            },
            started_at=self._clock(),
        )
        self.records.append(record)
        self._seen[key] = record
        self._metrics.mark(ResearchMetrics.FIRST_SEARCH)
        self._emitter.search_started(index=index, query=record.query, **record.parameters)

        try:
            response = await self._client.search(
                record.query,
                max_results=self.budget.max_results_per_search,
                topic=args["topic"],
                search_depth=depth,
                time_range=args["time_range"],
            )
        except TavilyError as exc:
            record.completed_at = self._clock()
            record.error = exc.code
            self._emitter.search_completed(
                index=index, query=record.query, result_count=0, error=exc.code
            )
            log(logger, logging.WARNING, "search failed", index=index, error=str(exc))
            # Surfaced to the model so it can say evidence is unavailable (PLAN §22)
            # rather than inventing one; the UI already saw the error on the event.
            return f"Search failed: {exc.user_message} Do not invent sources."

        credits = self.state.spend(depth)
        record.completed_at = self._clock()
        record.result_count = len(response.results)
        record.credit_usage = credits

        before = len(self._registry)
        sources = self._registry.add_all(response.results, record.query)
        record.new_source_ids = [s.id for s in sources if s.number > before]

        self._metrics.record_search(
            latency_ms=record.latency_ms or 0.0, credits=credits, advanced=depth == "advanced"
        )
        self._emitter.search_completed(
            index=index,
            query=record.query,
            result_count=record.result_count,
            new_source_ids=record.new_source_ids,
            total_sources=len(self._registry),
        )
        log(
            logger,
            logging.INFO,
            "search completed",
            index=index,
            query=record.query,
            depth=depth,
            results=record.result_count,
            latency_ms=round(record.latency_ms or 0.0, 1),
        )
        return self._render(sources, record, clamp_reason)

    def _render(
        self, sources: list[Source], record: SearchRecord, clamp_reason: str | None
    ) -> str:
        header = [f'Results for "{record.query}"']
        if clamp_reason:
            header.append(f"(depth was set to {record.parameters['search_depth']}: {clamp_reason})")
        if not sources:
            header.append("No results were returned for this query.")
            body = ""
        else:
            body = self._registry.render_blocks(sources)
        footer = (
            f"Searches used: {self.state.searches_used}/{self.budget.max_searches}. "
            "Cite these sources as [S1], [S2], … — never write a URL yourself."
        )
        return "\n".join(part for part in [" ".join(header), body, footer] if part).strip()
