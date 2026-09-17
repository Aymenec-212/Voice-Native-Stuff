"""Normalized search results and per-session source IDs (docs/PLAN.md §12).

Results are reduced to the five fields the model actually needs and given stable IDs
``[S1] [S2] …`` for the session. Nothing else from the Tavily payload reaches the model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

#: Snippets are truncated before they reach the model so one verbose result cannot
#: crowd out the others.
MAX_SNIPPET_CHARS = 1200


@dataclass(frozen=True)
class SearchResult:
    """A single normalized Tavily result."""

    title: str
    url: str
    content: str
    score: float | None = None
    published_date: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "url": self.url,
            "content": self.content,
            "score": self.score,
            "published_date": self.published_date,
        }


@dataclass(frozen=True)
class Source:
    """A :class:`SearchResult` that has been given a session-stable ID."""

    id: str
    number: int
    result: SearchResult
    query: str

    @property
    def title(self) -> str:
        return self.result.title

    @property
    def url(self) -> str:
        return self.result.url

    def to_prompt_block(self) -> str:
        lines = [f"[{self.id}]", f"Title: {self.title}", f"URL: {self.url}"]
        if self.result.published_date:
            lines.append(f"Published: {self.result.published_date}")
        if self.result.score is not None:
            lines.append(f"Relevance: {self.result.score:.2f}")
        lines.append(f"Content: {self.result.content}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "number": self.number, "query": self.query, **self.result.to_dict()}


def canonical_url(url: str) -> str:
    """Key used for de-duplication. The original URL is always what we display."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    scheme = (parts.scheme or "https").lower()
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((scheme, netloc, path, parts.query, ""))


def truncate(text: str, limit: int = MAX_SNIPPET_CHARS) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


class SourceRegistry:
    """Assigns ``S1, S2, …`` across a whole research session and de-duplicates by URL."""

    def __init__(self) -> None:
        self._sources: list[Source] = []
        self._by_id: dict[str, Source] = {}
        self._by_url: dict[str, Source] = {}

    def __len__(self) -> int:
        return len(self._sources)

    def add(self, result: SearchResult, query: str) -> tuple[Source, bool]:
        """Register *result*. Returns ``(source, is_new)``; repeats keep their first ID."""
        key = canonical_url(result.url)
        existing = self._by_url.get(key)
        if existing is not None:
            return existing, False
        number = len(self._sources) + 1
        source = Source(id=f"S{number}", number=number, result=result, query=query)
        self._sources.append(source)
        self._by_id[source.id] = source
        self._by_url[key] = source
        return source, True

    def add_all(self, results: list[SearchResult], query: str) -> list[Source]:
        return [self.add(result, query)[0] for result in results]

    def get(self, source_id: str) -> Source | None:
        return self._by_id.get(source_id.upper())

    def all(self) -> list[Source]:
        return list(self._sources)

    def render_blocks(self, sources: list[Source] | None = None) -> str:
        chosen = self._sources if sources is None else sources
        return "\n\n".join(source.to_prompt_block() for source in chosen)

    def render_index(self) -> str:
        """Compact ID → title/URL list injected before final synthesis."""
        return "\n".join(f"[{s.id}] {s.title} — {s.url}" for s in self._sources)


@dataclass
class SearchRecord:
    """One executed search, for the session object and metrics (PLAN §20)."""

    index: int
    query: str
    parameters: dict[str, Any] = field(default_factory=dict)
    started_at: float = 0.0
    completed_at: float | None = None
    result_count: int = 0
    credit_usage: int = 0
    new_source_ids: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def latency_ms(self) -> float | None:
        if self.completed_at is None:
            return None
        return (self.completed_at - self.started_at) * 1000.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "query": self.query,
            "parameters": self.parameters,
            "result_count": self.result_count,
            "credit_usage": self.credit_usage,
            "new_source_ids": list(self.new_source_ids),
            "latency_ms": None if self.latency_ms is None else round(self.latency_ms, 1),
            "error": self.error,
        }
