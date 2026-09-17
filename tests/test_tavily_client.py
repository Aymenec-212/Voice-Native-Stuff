import json

import httpx
import pytest

from vnr.config import TavilyConfig
from vnr.errors import TavilyAuthError, TavilyError, TavilyRateLimitError
from vnr.research.tavily import TavilyClient

CONFIG = TavilyConfig(api_key="tvly-test")


def client_with(handler) -> TavilyClient:
    return TavilyClient(CONFIG, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def test_retrieval_flags_are_forced_off():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        assert request.headers["authorization"] == "Bearer tvly-test"
        return httpx.Response(200, json={"results": [{"url": "https://a.test/1", "title": "A"}]})

    async with client_with(handler) as client:
        response = await client.search("q", max_results=5, time_range="week")

    assert seen["include_answer"] is False
    assert seen["include_images"] is False
    assert seen["include_raw_content"] is False
    assert seen["time_range"] == "week"
    assert len(response.results) == 1


async def test_time_range_is_omitted_when_unset():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"results": []})

    async with client_with(handler) as client:
        await client.search("q", max_results=5)
    assert "time_range" not in seen


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, TavilyAuthError),
        (403, TavilyAuthError),
        (429, TavilyRateLimitError),
        (432, TavilyRateLimitError),
        (500, TavilyError),
    ],
)
async def test_http_errors_map_to_typed_errors(status, expected):
    async with client_with(lambda r: httpx.Response(status, text="nope")) as client:
        with pytest.raises(expected):
            await client.search("q", max_results=5)


async def test_transport_failures_become_tavily_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    async with client_with(handler) as client:
        with pytest.raises(TavilyError):
            await client.search("q", max_results=5)
