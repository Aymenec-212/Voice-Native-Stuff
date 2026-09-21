"""Provider-layer tests over httpx.MockTransport — no network, no keys."""

import json

import httpx
import pytest

from vnr.config import NebiusConfig
from vnr.errors import NebiusAuthError, NebiusError
from vnr.research.nebius import MalformedToolCall, NebiusClient, ToolCall

CONFIG = NebiusConfig(api_key="test-key", model="nvidia/Nemotron-3_5-Lightning")


def client_with(handler) -> NebiusClient:
    transport = httpx.MockTransport(handler)
    return NebiusClient(
        CONFIG,
        client=httpx.AsyncClient(transport=transport),
        backoff_s=0,
        sleep=_no_sleep,
    )


async def _no_sleep(_seconds: float) -> None:
    return None


def chat_response(message: dict, usage: dict | None = None) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "choices": [{"message": message, "finish_reason": message.get("_finish", "stop")}],
            "usage": usage or {},
        },
    )


async def test_tool_calls_are_parsed():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == CONFIG.model
        assert body["tools"][0]["function"]["name"] == "web_search"
        assert request.headers["authorization"] == "Bearer test-key"
        return chat_response(
            {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query": "kyutai stt"}',
                        },
                    }
                ],
                "_finish": "tool_calls",
            },
            usage={"prompt_tokens": 12, "completion_tokens": 3},
        )

    async with client_with(handler) as client:
        completion = await client.create_tool_completion(
            [{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "web_search"}}],
        )

    assert completion.wants_tools
    call = completion.tool_calls[0]
    assert call.arguments() == {"query": "kyutai stt"}
    assert completion.usage["prompt_tokens"] == 12
    assert completion.to_assistant_message()["tool_calls"][0]["function"]["name"] == "web_search"


def test_malformed_tool_arguments_raise():
    with pytest.raises(MalformedToolCall):
        ToolCall(id="c", name="web_search", raw_arguments="{not json").arguments()
    with pytest.raises(MalformedToolCall):
        ToolCall(id="c", name="web_search", raw_arguments='["a"]').arguments()
    assert ToolCall(id="c", name="web_search", raw_arguments="").arguments() == {}


async def test_extra_body_is_merged_into_requests():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return chat_response({"content": "ok"})

    config = NebiusConfig(api_key="k", extra_body={"chat_template_kwargs": {"thinking": False}})
    client = NebiusClient(config, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    async with client:
        await client.create_completion([{"role": "user", "content": "hi"}])
    assert seen["chat_template_kwargs"] == {"thinking": False}


async def test_streaming_separates_content_and_reasoning():
    stream = (
        'data: {"choices":[{"delta":{"reasoning_content":"hidden thinking"}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"Hello "}}]}\n\n'
        'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}\n\n'
        "data: [DONE]\n\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, text=stream, headers={"content-type": "text/event-stream"})

    text, reasoning, usage = "", "", {}
    async with client_with(handler) as client:
        async for delta in client.stream_completion([{"role": "user", "content": "hi"}]):
            text += delta.text
            reasoning += delta.reasoning
            usage = delta.usage or usage

    assert text == "Hello world"
    assert reasoning == "hidden thinking"
    assert usage["completion_tokens"] == 2


async def test_stream_falls_back_when_stream_options_are_rejected():
    attempts = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        attempts.append("stream_options" in body)
        if "stream_options" in body:
            return httpx.Response(400, text="unsupported parameter: stream_options")
        return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')

    async with client_with(handler) as client:
        text = "".join([d.text async for d in client.stream_completion([])])

    assert attempts == [True, False]
    assert text == "ok"


async def test_auth_errors_are_not_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(401, text="invalid key")

    async with client_with(handler) as client:
        with pytest.raises(NebiusAuthError):
            await client.create_completion([])
    assert len(calls) == 1


async def test_transient_errors_are_retried():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(429, text="slow down")
        return chat_response({"content": "finally"})

    async with client_with(handler) as client:
        completion = await client.create_completion([])

    assert len(calls) == 3
    assert completion.content == "finally"


async def test_retries_are_bounded():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(503, text="unavailable")

    async with client_with(handler) as client:
        with pytest.raises(NebiusError):
            await client.create_completion([])
    assert len(calls) == 3  # initial attempt + 2 retries


async def test_list_models():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}, {}]})

    async with client_with(handler) as client:
        assert await client.list_models() == ["a", "b"]
