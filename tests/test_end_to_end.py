"""Whole-pipeline test: CLI wiring → agent → session object, over a mocked transport.

Both providers are served by one httpx.MockTransport, routed by host.
"""

import json

import httpx

from vnr.config import NebiusConfig, ResearchBudget, Settings, TavilyConfig
from vnr.events import EventRecorder, EventType
from vnr.research.runner import run_research

SETTINGS = Settings(
    nebius=NebiusConfig(api_key="nebius-test"),
    tavily=TavilyConfig(api_key="tvly-test"),
    budget=ResearchBudget(max_searches=2, max_turns=4),
)


def build_transport() -> tuple[httpx.MockTransport, list[str]]:
    seen: list[str] = []
    turns = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "tavily" in request.url.host:
            query = json.loads(request.content)["query"]
            return httpx.Response(
                200,
                json={
                    "query": query,
                    "results": [
                        {
                            "title": "Kyutai STT",
                            "url": "https://kyutai.org/stt",
                            "content": "Streaming speech-to-text, 1B params.",
                            "score": 0.91,
                            "published_date": "2026-06-01",
                        }
                    ],
                },
            )

        body = json.loads(request.content)
        if body.get("stream"):
            chunks = [
                'data: {"choices":[{"delta":{"content":"Kyutai STT streams audio [S"}}]}',
                'data: {"choices":[{"delta":{"content":"1]. It is small."}}]}',
                'data: {"choices":[],"usage":{"prompt_tokens":900,"completion_tokens":40}}',
                "data: [DONE]",
            ]
            return httpx.Response(200, text="\n\n".join(chunks) + "\n\n")

        turns["n"] += 1
        if turns["n"] == 1:
            message = {
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "web_search",
                            "arguments": '{"query": "kyutai stt streaming"}',
                        },
                    }
                ],
            }
            finish = "tool_calls"
        else:
            message = {"content": "Evidence is sufficient."}
            finish = "stop"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 500, "completion_tokens": 20},
            },
        )

    return httpx.MockTransport(handler), seen


async def test_full_pipeline_produces_a_cited_session():
    transport, seen = build_transport()
    recorder = EventRecorder()

    async with httpx.AsyncClient(transport=transport) as http:
        session, result = await run_research(
            "what is kyutai stt?", SETTINGS, sink=recorder, http_client=http
        )

    assert any("tavily" in url for url in seen)
    assert session.status.value == "COMPLETED"
    assert session.submitted_query == "what is kyutai stt?"
    assert session.answer.startswith("Kyutai STT streams audio [1]. It is small.")
    assert "Sources\n[1] Kyutai STT — https://kyutai.org/stt" in session.answer
    assert len(session.sources) == 1
    assert session.searches[0].credit_usage == 1
    assert session.research_metrics.input_tokens == 1900  # 2 decision turns + synthesis
    assert session.research_metrics.output_tokens == 80
    assert recorder.of_type(EventType.RESEARCH_COMPLETED)

    dumped = json.loads(json.dumps(session.to_dict()))  # must be JSON-serializable
    assert dumped["metrics"]["research"]["tavily_credits"] == 1
    assert dumped["transcript_was_edited"] is False


async def test_session_records_the_edit_between_transcript_and_query():
    from vnr.session import ResearchSession

    transport, _ = build_transport()
    session = ResearchSession(raw_transcript="what is kyoto ai stt?")

    async with httpx.AsyncClient(transport=transport) as http:
        session, _ = await run_research(
            "what is kyutai stt?", SETTINGS, session=session, http_client=http
        )

    assert session.transcript_was_edited is True
