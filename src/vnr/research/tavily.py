"""Tavily retrieval client and response normalization (docs/PLAN.md §10, §12).

Retrieval only: ``include_answer`` is forced off so Tavily never answers on the model's
behalf. Tavily retrieves, Nemotron reasons.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import TavilyConfig
from ..errors import TavilyAuthError, TavilyError, TavilyRateLimitError
from .sources import SearchResult, truncate

TOPICS = ("general", "news", "finance")
TIME_RANGES = ("day", "week", "month", "year")
DEPTHS = ("basic", "advanced")


@dataclass
class TavilyResponse:
    query: str
    results: list[SearchResult] = field(default_factory=list)
    response_time: float | None = None

    def __len__(self) -> int:
        return len(self.results)


def normalize_results(payload: dict[str, Any], max_results: int) -> list[SearchResult]:
    """Reduce a Tavily payload to the five fields the model needs (PLAN §12).

    Tolerant by design: a result missing a URL is dropped, everything else is coerced.
    """
    raw_results = payload.get("results")
    if not isinstance(raw_results, list):
        return []
    normalized: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue  # a source we cannot attribute is useless to us
        score = item.get("score")
        try:
            score_value = None if score is None else float(score)
        except (TypeError, ValueError):
            score_value = None
        published = item.get("published_date") or item.get("published_time")
        normalized.append(
            SearchResult(
                title=str(item.get("title") or url).strip(),
                url=url,
                content=truncate(str(item.get("content") or item.get("raw_content") or "")),
                score=score_value,
                published_date=str(published).strip() if published else None,
            )
        )
        if len(normalized) >= max_results:
            break
    return normalized


class TavilyClient:
    """Async Tavily Search client. The application, not the model, owns the parameters."""

    def __init__(self, config: TavilyConfig, *, client: httpx.AsyncClient | None = None) -> None:
        self.config = config
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> TavilyClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def search(
        self,
        query: str,
        *,
        max_results: int,
        topic: str = "general",
        search_depth: str = "basic",
        time_range: str | None = None,
    ) -> TavilyResponse:
        body: dict[str, Any] = {
            "query": query,
            "topic": topic,
            "search_depth": search_depth,
            "max_results": max_results,
            # Application-owned defaults (PLAN §10) — not exposed to the model.
            "include_answer": False,
            "include_images": False,
            "include_raw_content": False,
        }
        if time_range:
            body["time_range"] = time_range

        url = f"{self.config.base_url.rstrip('/')}/search"
        headers = {
            "Authorization": f"Bearer {self.config.require_key()}",
            "Content-Type": "application/json",
        }
        try:
            response = await self._client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise TavilyError(f"Tavily request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise TavilyError(f"Tavily request failed: {exc}") from exc

        if response.status_code >= 400:
            raise self._http_error(response)

        payload = response.json()
        if not isinstance(payload, dict):
            raise TavilyError("Tavily returned an unexpected payload")
        return TavilyResponse(
            query=str(payload.get("query") or query),
            results=normalize_results(payload, max_results),
            response_time=_maybe_float(payload.get("response_time")),
        )

    @staticmethod
    def _http_error(response: httpx.Response) -> TavilyError:
        detail = response.text[:300]
        if response.status_code in (401, 403):
            return TavilyAuthError(f"Tavily rejected the API key ({response.status_code})")
        if response.status_code in (429, 432, 433):
            return TavilyRateLimitError(f"Tavily rate/credit limit hit ({response.status_code})")
        return TavilyError(f"Tavily returned {response.status_code}: {detail}")


def _maybe_float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None
