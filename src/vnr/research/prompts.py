"""System prompt and synthesis instruction (docs/PLAN.md §16).

Concise and behavioral. Most of the intelligence should come from the model and the loop,
not from hundreds of prompt rules.
"""

from __future__ import annotations

from datetime import date, datetime

from ..config import ResearchBudget
from .sources import SourceRegistry


def time_context(*, today: date | None = None, now: datetime | None = None) -> str:
    """Read the clock at request time, never at process startup (or from search snippets)."""
    if today is not None:
        current = f"Today is {today.isoformat()}."
    else:
        now = now or datetime.now().astimezone()
        current = (
            f"Today is {now.date().isoformat()}. "
            f"Current local time: {now.isoformat(timespec='seconds')} "
            f"(timezone {now.tzname()}; the UTC offset is included)."
        )
    return current + (
        " Interpret today, tomorrow, next, upcoming and latest relative to this clock. "
        "For a next match or other scheduled event, verify the event date, year, kickoff "
        "time and timezone from a current official schedule. An event before this clock "
        "is past, not next. Include the current date/year in time-sensitive searches. "
        "Distinguish publication dates from event dates; stale snippets cannot establish "
        "what is next. If you cannot verify a future event, say so instead of guessing."
    )


def system_prompt(budget: ResearchBudget, *, today: date | None = None) -> str:
    return f"""{time_context(today=today)} You are the research engine behind a \
voice-driven macOS utility: the user spoke a request, reviewed the transcript, and \
approved it.

You have exactly one tool, `web_search`. You request it; the application executes it and \
returns numbered sources.

How to work:
- Search whenever the answer depends on facts you cannot verify from memory, and always \
for anything current, versioned, released, priced, or described as recent.
- Prefer several focused searches over one broad one. Keep queries short and specific.
- Prefer primary sources: official documentation for product claims, papers for research \
claims, recent sources when the user asks what is new.
- Note disagreement between sources rather than averaging it away. Snippets are evidence, \
not proof.
- Stop as soon as the evidence is sufficient. You may use at most {budget.max_searches} \
searches across {budget.max_turns} turns. Repeating a search you already ran is wasted.
- Cite only the source IDs the tool gave you, written as [S1], [S2]. Never write a URL and \
never cite an ID you were not given — the application builds the source list from the \
retrieved URLs.

Between searches, reply with a single short line saying what evidence is still missing, or \
that the evidence is now sufficient. Do not draft the answer until you are explicitly asked \
for the final answer."""


def synthesis_instruction(
    query: str, registry: SourceRegistry, *, today: date | None = None
) -> str:
    clock = time_context(today=today) + "\n\n"
    if not len(registry):
        return (
            clock + f'Write the final answer now for this request: "{query}"\n\n'
            "No sources were retrieved. Say plainly that you could not find supporting "
            "evidence, and do not present unverified claims as researched facts. Do not "
            "invent sources or URLs."
        )
    return (
        clock + f'Write the final answer now for this request: "{query}"\n\n'
        f"Sources available to you:\n{registry.render_index()}\n\n"
        "Rules:\n"
        "- Ground every factual claim in these sources using [S1]-style markers.\n"
        "- Use only the IDs listed above. Never write a URL and do not add a Sources "
        "section — the application appends one from the retrieved URLs.\n"
        "- If the evidence is thin, incomplete or contradictory, say so plainly.\n"
        "- Be concise unless the user asked for depth: a few short paragraphs or a tight "
        "list. No preamble and no restating of the question."
    )
