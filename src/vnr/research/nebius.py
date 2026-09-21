"""Thin Nebius Token Factory client (docs/PLAN.md §8).

OpenAI-compatible HTTP, spoken directly with ``httpx`` — no SDK, no orchestration
framework. Three verbs, exactly as the plan describes: ``create_completion``,
``create_tool_completion`` and ``stream_completion``.

Note on reasoning models: some Token Factory models return a separate
``reasoning_content`` field; others wrap it in <think> tags. Both are separated
from the answer and preserved for the UI's collapsed reasoning disclosure.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config import NebiusConfig
from ..errors import NebiusAuthError, NebiusError
from ..logging import get_logger, log
from .reasoning import ReasoningSplitter

logger = get_logger("nebius")

RETRYABLE_STATUS = {408, 409, 429, 500, 502, 503, 504}


class MalformedToolCall(Exception):
    """The model asked for a tool call we cannot parse or do not expose."""


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    raw_arguments: str

    def arguments(self) -> dict[str, Any]:
        raw = (self.raw_arguments or "").strip()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MalformedToolCall(f"arguments were not valid JSON: {exc.msg}") from exc
        if not isinstance(parsed, dict):
            raise MalformedToolCall("arguments must be a JSON object")
        return parsed

    def to_message_part(self) -> dict[str, Any]:
        """Shape required when echoing the assistant turn back into the conversation."""
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.raw_arguments},
        }


@dataclass
class Completion:
    content: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)

    def to_assistant_message(self) -> dict[str, Any]:
        message: dict[str, Any] = {"role": "assistant", "content": self.content or ""}
        if self.reasoning:
            message["reasoning_content"] = self.reasoning
        if self.tool_calls:
            message["tool_calls"] = [tc.to_message_part() for tc in self.tool_calls]
        return message


@dataclass
class StreamDelta:
    text: str = ""
    reasoning: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    finish_reason: str = ""


class NebiusClient:
    """Minimal async client for the Token Factory chat-completions endpoint."""

    def __init__(
        self,
        config: NebiusConfig,
        *,
        client: httpx.AsyncClient | None = None,
        max_retries: int = 2,
        backoff_s: float = 1.0,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.config = config
        self._max_retries = max_retries
        self._backoff_s = backoff_s
        self._sleep = sleep
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=config.timeout_s)

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def __aenter__(self) -> NebiusClient:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -- public API ----------------------------------------------------------------
    async def list_models(self) -> list[str]:
        """``GET /v1/models`` — the documented way to check a model ID exists (PLAN §8)."""
        response = await self._request("GET", "/models")
        data = response.json().get("data") or []
        return [str(item.get("id", "")) for item in data if item.get("id")]

    async def create_completion(
        self, messages: Sequence[dict[str, Any]], **options: Any
    ) -> Completion:
        payload = self._payload(messages, **options)
        response = await self._request("POST", "/chat/completions", json=payload)
        return self._parse_completion(response.json())

    async def create_tool_completion(
        self,
        messages: Sequence[dict[str, Any]],
        tools: Sequence[dict[str, Any]],
        *,
        tool_choice: str = "auto",
        **options: Any,
    ) -> Completion:
        return await self.create_completion(
            messages, tools=list(tools), tool_choice=tool_choice, **options
        )

    async def stream_completion(
        self, messages: Sequence[dict[str, Any]], **options: Any
    ) -> AsyncIterator[StreamDelta]:
        """Yield text deltas for the final synthesis (PLAN §17)."""
        payload = self._payload(messages, stream=True, **options)
        payload.setdefault("stream_options", {"include_usage": True})
        try:
            async for delta in self._stream(payload):
                yield delta
        except NebiusError as exc:
            # Some deployments reject stream_options; retry once without it.
            if "stream_options" not in str(exc):
                raise
            payload.pop("stream_options", None)
            async for delta in self._stream(payload):
                yield delta

    # -- internals -----------------------------------------------------------------
    def _payload(self, messages: Sequence[dict[str, Any]], **options: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": list(messages),
        }
        payload.update({k: v for k, v in options.items() if v is not None})
        for key, value in (self.config.extra_body or {}).items():
            payload.setdefault(key, value)
        return payload

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.require_key()}",
            "Content-Type": "application/json",
        }

    def _url(self, path: str) -> str:
        return f"{self.config.base_url.rstrip('/')}{path}"

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = await self._client.request(
                    method, self._url(path), headers=self._headers(), **kwargs
                )
            except httpx.TimeoutException as exc:
                last_error = NebiusError(f"Nebius request timed out: {exc}")
            except httpx.HTTPError as exc:
                last_error = NebiusError(f"Nebius request failed: {exc}")
            else:
                if response.status_code < 400:
                    return response
                error = self._http_error(response)
                if response.status_code not in RETRYABLE_STATUS:
                    raise error
                last_error = error
            if attempt < self._max_retries:
                log(
                    logger,
                    logging.WARNING,
                    "nebius retry",
                    attempt=attempt + 1,
                    reason=str(last_error),
                )
                await self._sleep(self._backoff_s * (2**attempt))
        raise last_error or NebiusError("Nebius request failed")

    def _http_error(self, response: httpx.Response) -> NebiusError:
        detail = response.text[:500]
        if response.status_code in (401, 403):
            return NebiusAuthError(f"Nebius rejected the API key ({response.status_code})")
        return NebiusError(f"Nebius returned {response.status_code}: {detail}")

    def _parse_completion(self, body: dict[str, Any]) -> Completion:
        choices = body.get("choices") or []
        if not choices:
            raise NebiusError("Nebius returned no choices")
        choice = choices[0]
        message = choice.get("message") or {}
        tool_calls = []
        for item in message.get("tool_calls") or []:
            function = item.get("function") or {}
            tool_calls.append(
                ToolCall(
                    id=str(item.get("id") or f"call_{len(tool_calls)}"),
                    name=str(function.get("name") or ""),
                    raw_arguments=str(function.get("arguments") or ""),
                )
            )
        content, tagged_reasoning = ReasoningSplitter().feed(
            str(message.get("content") or ""), final=True
        )
        return Completion(
            content=content,
            reasoning=str(message.get("reasoning_content") or message.get("reasoning") or "")
            + tagged_reasoning,
            tool_calls=tool_calls,
            finish_reason=str(choice.get("finish_reason") or ""),
            usage=body.get("usage") or {},
        )

    async def _stream(self, payload: dict[str, Any]) -> AsyncIterator[StreamDelta]:
        splitter = ReasoningSplitter()
        try:
            async with self._client.stream(
                "POST", self._url("/chat/completions"), headers=self._headers(), json=payload
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise self._http_error(response)
                async for line in response.aiter_lines():
                    delta = _parse_sse_line(line)
                    if delta is _DONE:
                        break
                    if delta is not None:
                        delta.text, tagged = splitter.feed(delta.text)
                        delta.reasoning += tagged
                        yield delta
                text, reasoning = splitter.feed("", final=True)
                if text or reasoning:
                    yield StreamDelta(text=text, reasoning=reasoning)
        except httpx.TimeoutException as exc:
            raise NebiusError(f"Nebius stream timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise NebiusError(f"Nebius stream failed: {exc}") from exc


_DONE = StreamDelta(finish_reason="__done__")


def _parse_sse_line(line: str) -> StreamDelta | None:
    """Parse one ``text/event-stream`` line into a delta, ``None``, or the DONE sentinel."""
    line = line.strip()
    if not line or not line.startswith("data:"):
        return None
    data = line[len("data:") :].strip()
    if data == "[DONE]":
        return _DONE
    try:
        chunk = json.loads(data)
    except json.JSONDecodeError:
        return None
    choices = chunk.get("choices") or []
    delta = StreamDelta(usage=chunk.get("usage") or {})
    if choices:
        payload = choices[0].get("delta") or {}
        delta.reasoning = str(payload.get("reasoning_content") or payload.get("reasoning") or "")
        delta.text = str(payload.get("content") or "")
        delta.finish_reason = str(choices[0].get("finish_reason") or "")
    return delta
