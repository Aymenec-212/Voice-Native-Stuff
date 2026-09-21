from datetime import UTC, datetime, timedelta, timezone

from vnr.config import ResearchBudget
from vnr.research.prompts import synthesis_instruction, system_prompt, time_context
from vnr.research.sources import SourceRegistry


def test_clock_includes_local_date_offset_and_rejects_past_upcoming_events():
    now = datetime(2026, 9, 21, 0, 30, tzinfo=timezone(timedelta(hours=1)))
    text = time_context(now=now)
    assert "2026-09-21T00:30:00+01:00" in text
    assert "past, not next" in text
    assert "publication dates from event dates" in text
    assert "current official schedule" in text


def test_date_refreshes_at_synthesis_across_midnight(monkeypatch):
    from vnr.research import prompts

    class Clock:
        value = datetime(2026, 9, 20, 23, 59, tzinfo=UTC)

        @classmethod
        def now(cls):
            return cls.value

    monkeypatch.setattr(prompts, "datetime", Clock)
    assert "Today is 2026-09-20" in system_prompt(ResearchBudget())
    Clock.value += timedelta(minutes=2)
    assert "Today is 2026-09-21" in synthesis_instruction("next match", SourceRegistry())
