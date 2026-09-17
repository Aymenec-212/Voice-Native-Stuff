"""Opt-in integration tests against the real Nebius and Tavily APIs (docs/PLAN.md §24).

These spend real tokens and Tavily credits, so they are skipped unless asked for:

    VNR_RUN_INTEGRATION=1 uv run pytest tests/integration -v -s

Run them on the Mac, where .env has the keys. They assert on *shape* — bounded searches,
valid citations, no invented URLs — not on wording, which a model is free to vary.
"""

from __future__ import annotations

import os

import pytest

from vnr.config import Settings
from vnr.events import EventRecorder, EventType
from vnr.research.nebius import NebiusClient
from vnr.research.runner import run_research

pytestmark = pytest.mark.skipif(
    os.getenv("VNR_RUN_INTEGRATION") != "1",
    reason="set VNR_RUN_INTEGRATION=1 to run tests that spend real API credits",
)

PROMPTS = [
    "What's new in Kyutai STT?",
    "Find recent work on streaming speech recognition.",
    "What did NVIDIA recently release around agentic models?",
]


@pytest.fixture(scope="module")
def settings() -> Settings:
    loaded = Settings.load()
    loaded.nebius.require_key()
    loaded.tavily.require_key()
    return loaded


async def test_the_configured_model_exists_on_this_account(settings: Settings):
    """PLAN §8: verify the model ID rather than assuming it."""
    async with NebiusClient(settings.nebius) as client:
        models = await client.list_models()
    assert settings.nebius.model in models, (
        f"{settings.nebius.model!r} is not available. Available: {sorted(models)[:20]}"
    )


@pytest.mark.parametrize("prompt", PROMPTS)
async def test_research_produces_a_bounded_cited_answer(settings: Settings, prompt: str):
    recorder = EventRecorder()
    session, result = await run_research(prompt, settings, sink=recorder)

    assert result.answer.strip(), "the agent produced no answer"
    assert result.turns <= settings.budget.max_turns
    assert len(result.searches) <= settings.budget.max_searches

    retrieved = {source.url for source in result.sources}
    for source in result.cited:
        assert source.url in retrieved, "a citation pointed outside the retrieved sources"
    assert not result.citation_report.invalid_ids, (
        f"model invented source IDs: {result.citation_report.invalid_ids}"
    )
    assert "http" not in result.answer.split("Sources")[0], (
        "the body contains a raw URL — the model should only write [n] markers"
    )

    assert recorder.of_type(EventType.RESEARCH_COMPLETED)
    print(f"\n--- {prompt}\n{result.answer}\n")
    print(session.research_metrics.to_dict())


async def test_a_simple_lookup_does_not_burn_the_whole_budget(settings: Settings):
    """A one-fact question should not trigger four searches (PLAN §11)."""
    _session, result = await run_research(
        "What is the capital of Morocco?", settings, sink=EventRecorder()
    )
    assert len(result.searches) <= 2, f"used {len(result.searches)} searches for a simple lookup"
