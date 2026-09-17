"""Mocked agent-loop tests (docs/PLAN.md §24): deterministic LLM and Tavily sequences."""

import asyncio

import pytest

from vnr.config import ResearchBudget
from vnr.errors import NebiusAuthError
from vnr.events import EventType
from vnr.research.nebius import Completion, ToolCall
from vnr.research.tavily import TavilyResponse

from .conftest import FakeNebius, FakeTavily, make_agent, result, search_completion, tool_call


async def test_two_searches_then_a_cited_answer():
    nebius = FakeNebius(
        completions=[
            search_completion(tool_call("streaming ASR low-resource")),
            search_completion(tool_call("kyutai stt architecture", call_id="call_2")),
            Completion(content="Evidence is sufficient.", finish_reason="stop"),
        ],
        answer_chunks=["Kyutai streams audio [S", "1]. It is 1B params [S3]."],
    )
    tavily = FakeTavily(
        responses=[
            TavilyResponse("a", [result(1), result(2)]),
            TavilyResponse("b", [result(3)]),
        ]
    )
    agent, recorder, registry = make_agent(nebius, tavily)

    outcome = await agent.run("compare streaming ASR approaches")

    assert len(tavily.calls) == 2
    assert outcome.turns == 3
    assert outcome.stop_reason == "evidence_sufficient"
    assert len(registry) == 3
    assert outcome.answer.startswith("Kyutai streams audio [1]. It is 1B params [2].")
    assert "Sources\n[1] Result 1 — https://example.com/article-1" in outcome.answer
    assert "[2] Result 3 — https://example.com/article-3" in outcome.answer
    assert [s.id for s in outcome.cited] == ["S1", "S3"]


async def test_the_ui_sees_a_complete_ordered_event_sequence():
    nebius = FakeNebius(
        completions=[search_completion(tool_call("q")), Completion(content="done")],
        answer_chunks=["Answer [S1]."],
    )
    agent, recorder, _ = make_agent(nebius, FakeTavily())

    await agent.run("q")

    research_events = [t for t in recorder.types() if not t.startswith("session.")]
    assert research_events == [
        EventType.RESEARCH_STARTED.value,
        EventType.RESEARCH_SEARCH_STARTED.value,
        EventType.RESEARCH_SEARCH_COMPLETED.value,
        EventType.RESEARCH_SYNTHESIZING.value,
        EventType.RESEARCH_ANSWER_DELTA.value,  # the answer text
        EventType.RESEARCH_ANSWER_DELTA.value,  # the sources section
        EventType.RESEARCH_COMPLETED.value,
    ]
    states = [e.data["state"] for e in recorder.of_type(EventType.STATE_CHANGED)]
    assert states == ["RESEARCH_STARTED", "SYNTHESIZING", "ANSWER_STREAMING", "COMPLETED"]

    completed = recorder.of_type(EventType.RESEARCH_COMPLETED)[0].data
    assert completed["searches"] == 1
    assert completed["cited_sources"] == [
        {"number": 1, "id": "S1", "title": "Result 1", "url": "https://example.com/article-1"}
    ]
    assert completed["metrics"]["go_to_first_search_ms"] is not None


async def test_answer_deltas_reconstruct_the_answer():
    nebius = FakeNebius(
        completions=[Completion(content="no search needed")],
        answer_chunks=["Short ", "answer."],
    )
    agent, recorder, _ = make_agent(nebius, FakeTavily())
    outcome = await agent.run("q")
    assert recorder.answer_text() == outcome.answer == "Short answer."


async def test_turn_limit_terminates_the_loop():
    """The model never stops asking; the application must (PLAN §11)."""
    nebius = FakeNebius(
        completions=[search_completion(tool_call(f"q{i}", call_id=f"c{i}")) for i in range(10)],
        answer_chunks=["Partial [S1]."],
    )
    tavily = FakeTavily()
    agent, _, _ = make_agent(nebius, tavily, budget=ResearchBudget(max_turns=3, max_searches=10))

    outcome = await agent.run("endless")

    assert outcome.turns == 3
    assert outcome.stop_reason == "turn_limit_reached"
    assert len(tavily.calls) == 3
    assert outcome.answer  # an answer is still produced


async def test_search_budget_stops_the_loop_before_the_turn_limit():
    nebius = FakeNebius(
        completions=[search_completion(tool_call(f"q{i}", call_id=f"c{i}")) for i in range(10)],
        answer_chunks=["Answer [S1]."],
    )
    tavily = FakeTavily()
    agent, _, _ = make_agent(nebius, tavily, budget=ResearchBudget(max_turns=6, max_searches=2))

    outcome = await agent.run("endless")

    assert outcome.stop_reason == "search_budget_exhausted"
    assert len(tavily.calls) == 2
    assert outcome.turns == 3


async def test_malformed_tool_arguments_are_fed_back_not_fatal():
    nebius = FakeNebius(
        completions=[
            search_completion(ToolCall(id="c1", name="web_search", raw_arguments="{oops")),
            search_completion(tool_call("recovered", call_id="c2")),
            Completion(content="done"),
        ],
        answer_chunks=["Answer [S1]."],
    )
    tavily = FakeTavily()
    agent, _, _ = make_agent(nebius, tavily)

    outcome = await agent.run("q")

    tool_messages = [
        m for turn in nebius.messages_seen for m in turn if m.get("role") == "tool"
    ]
    assert any("could not be executed" in m["content"] for m in tool_messages)
    assert len(tavily.calls) == 1  # the malformed call never reached Tavily
    assert outcome.stop_reason == "evidence_sufficient"


async def test_unknown_tools_are_refused():
    nebius = FakeNebius(
        completions=[
            search_completion(ToolCall(id="c1", name="run_shell", raw_arguments='{"cmd":"ls"}')),
            Completion(content="done"),
        ],
        answer_chunks=["Answer."],
    )
    tavily = FakeTavily()
    agent, _, _ = make_agent(nebius, tavily)

    await agent.run("q")

    tool_messages = [
        m for turn in nebius.messages_seen for m in turn if m.get("role") == "tool"
    ]
    assert any("Unknown tool" in m["content"] for m in tool_messages)
    assert tavily.calls == []


async def test_invented_citations_are_dropped_from_the_answer():
    nebius = FakeNebius(
        completions=[search_completion(tool_call("q")), Completion(content="done")],
        answer_chunks=["Real [S1]. Invented [S42]."],
    )
    agent, recorder, _ = make_agent(nebius, FakeTavily())

    outcome = await agent.run("q")

    assert "[S42]" not in outcome.answer
    assert "https://" in outcome.answer  # the real source still made it
    assert outcome.citation_report.invalid_ids == ["S42"]
    completed = recorder.of_type(EventType.RESEARCH_COMPLETED)[0].data
    assert completed["invalid_citation_ids"] == ["S42"]


async def test_an_answer_with_no_sources_says_so_without_a_sources_list():
    nebius = FakeNebius(
        completions=[Completion(content="no search needed")],
        answer_chunks=["I could not find supporting evidence."],
    )
    tavily = FakeTavily()
    agent, _, registry = make_agent(nebius, tavily)

    outcome = await agent.run("something unsearchable")

    assert tavily.calls == []
    assert len(registry) == 0
    assert "Sources" not in outcome.answer


async def test_final_synthesis_withholds_the_tool_and_lists_the_sources():
    nebius = FakeNebius(
        completions=[search_completion(tool_call("q")), Completion(content="done")],
        answer_chunks=["Answer [S1]."],
    )
    agent, _, _ = make_agent(nebius, FakeTavily())

    await agent.run("q")

    assert nebius.stream_options_seen[0]["tool_choice"] == "none"
    synthesis_prompt = nebius.messages_seen[-1][-1]["content"]
    assert "[S1] Result 1 — https://example.com/article-1" in synthesis_prompt
    assert "do not add a Sources section" in synthesis_prompt


async def test_token_usage_is_accumulated():
    nebius = FakeNebius(
        completions=[
            Completion(content="done", usage={"prompt_tokens": 100, "completion_tokens": 10})
        ],
        answer_chunks=["Answer."],
        stream_usage={"prompt_tokens": 200, "completion_tokens": 50},
    )
    agent, _, _ = make_agent(nebius, FakeTavily())
    outcome = await agent.run("q")
    assert outcome.metrics.input_tokens == 300
    assert outcome.metrics.output_tokens == 60


async def test_provider_failures_emit_a_failed_event_and_propagate():
    class Failing(FakeNebius):
        async def create_tool_completion(self, messages, tools, **kwargs):
            raise NebiusAuthError()

    agent, recorder, _ = make_agent(Failing(), FakeTavily())

    with pytest.raises(NebiusAuthError):
        await agent.run("q")

    failed = recorder.of_type(EventType.RESEARCH_FAILED)[0].data
    assert failed["code"] == "nebius_auth"
    assert recorder.of_type(EventType.STATE_CHANGED)[-1].data["state"] == "FAILED"


async def test_cancellation_is_reported_and_propagates():
    class Cancelling(FakeNebius):
        async def create_tool_completion(self, messages, tools, **kwargs):
            raise asyncio.CancelledError()

    agent, recorder, _ = make_agent(Cancelling(), FakeTavily())

    with pytest.raises(asyncio.CancelledError):
        await agent.run("q")

    assert recorder.of_type(EventType.RESEARCH_CANCELLED)
    assert recorder.of_type(EventType.STATE_CHANGED)[-1].data["state"] == "CANCELLED"


async def test_the_system_prompt_states_the_budget_and_the_date():
    nebius = FakeNebius(completions=[Completion(content="done")], answer_chunks=["x"])
    agent, _, _ = make_agent(nebius, FakeTavily(), budget=ResearchBudget(max_searches=4))
    await agent.run("q")
    system = nebius.messages_seen[0][0]["content"]
    assert "2026-09-17" in system
    assert "at most 4 searches" in system
    assert "never write a url" in system.lower()
