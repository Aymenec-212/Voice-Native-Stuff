import pytest

from vnr.config import ResearchBudget
from vnr.errors import TavilyRateLimitError
from vnr.events import EventType
from vnr.research.tavily import TavilyResponse
from vnr.research.tools import ToolArgumentError, validate_arguments

from .conftest import FakeTavily, make_tool, result


async def test_search_emits_events_and_registers_sources():
    tavily = FakeTavily(responses=[TavilyResponse("q", [result(1), result(2)])])
    tool, registry, recorder, metrics = make_tool(tavily)

    content = await tool.run({"query": "  kyutai   stt  "})

    assert tavily.calls[0]["query"] == "kyutai stt"  # whitespace normalized
    assert tavily.calls[0]["max_results"] == 5
    assert "[S1]" in content and "[S2]" in content
    assert "Searches used: 1/4" in content
    assert len(registry) == 2
    assert recorder.types() == [
        EventType.RESEARCH_SEARCH_STARTED.value,
        EventType.RESEARCH_SEARCH_COMPLETED.value,
    ]
    assert recorder.events[1].data["new_source_ids"] == ["S1", "S2"]
    assert metrics.searches == 1
    assert metrics.tavily_credits == 1


async def test_application_owns_the_retrieval_parameters():
    """The model never gets to turn Tavily's own answer on (PLAN §10)."""
    tavily = FakeTavily()
    tool, *_ = make_tool(tavily)
    await tool.run({"query": "q", "include_answer": True, "max_results": 50})
    assert "include_answer" not in tavily.calls[0]
    assert tavily.calls[0]["max_results"] == 5


async def test_search_budget_is_enforced():
    tavily = FakeTavily()
    tool, _, recorder, _ = make_tool(tavily, budget=ResearchBudget(max_searches=2))

    for i in range(2):
        await tool.run({"query": f"query {i}"})
    assert tool.state.exhausted

    content = await tool.run({"query": "one too many"})
    assert "budget exhausted" in content.lower()
    assert len(tavily.calls) == 2  # no third API call was made
    assert len(recorder.of_type(EventType.RESEARCH_SEARCH_STARTED)) == 2


async def test_repeat_queries_do_not_spend_credits():
    tavily = FakeTavily()
    tool, *_ = make_tool(tavily)
    await tool.run({"query": "same thing"})
    content = await tool.run({"query": "Same Thing"})
    assert "already run" in content
    assert len(tavily.calls) == 1
    assert tool.state.searches_used == 1


async def test_advanced_depth_is_rationed():
    tavily = FakeTavily()
    tool, *_ = make_tool(tavily, budget=ResearchBudget(max_advanced_searches=1))

    await tool.run({"query": "a", "search_depth": "advanced"})
    assert tavily.calls[0]["search_depth"] == "advanced"
    assert tool.state.advanced_used == 1

    content = await tool.run({"query": "b", "search_depth": "advanced"})
    assert tavily.calls[1]["search_depth"] == "basic"
    assert "advanced search budget spent" in content


async def test_advanced_depth_can_be_disabled_entirely():
    tavily = FakeTavily()
    tool, *_ = make_tool(tavily, budget=ResearchBudget(allow_advanced=False))
    await tool.run({"query": "a", "search_depth": "advanced"})
    assert tavily.calls[0]["search_depth"] == "basic"


async def test_advanced_search_costs_two_credits():
    tavily = FakeTavily()
    tool, _, _, metrics = make_tool(tavily)
    await tool.run({"query": "a", "search_depth": "advanced"})
    assert metrics.tavily_credits == 2
    assert metrics.advanced_searches == 1


async def test_tavily_failure_is_reported_not_faked():
    tavily = FakeTavily(responses=[TavilyRateLimitError()])
    tool, registry, recorder, metrics = make_tool(tavily)

    content = await tool.run({"query": "q"})

    assert "Search unavailable" in content
    assert "Do not invent sources." in content
    assert len(registry) == 0
    assert metrics.searches == 0  # a failed search spends no credit
    assert recorder.events[-1].data["error"] == "tavily_rate_limit"
    assert tool.records[-1].error == "tavily_rate_limit"


async def test_empty_result_sets_are_stated_plainly():
    tavily = FakeTavily(responses=[TavilyResponse("q", [])])
    tool, *_ = make_tool(tavily)
    content = await tool.run({"query": "obscure"})
    assert "No results were returned" in content


def test_argument_validation():
    cleaned = validate_arguments({"query": "x", "topic": "news", "time_range": "week"})
    assert cleaned == {
        "query": "x",
        "topic": "news",
        "time_range": "week",
        "search_depth": None,
    }
    # unknown enum values fall back rather than failing the turn
    loose = validate_arguments({"query": "x", "topic": "sports", "time_range": "decade"})
    assert loose["topic"] == "general"
    assert loose["time_range"] is None

    for bad in ({}, {"query": ""}, {"query": "   "}, {"query": 5}):
        with pytest.raises(ToolArgumentError):
            validate_arguments(bad)
