"""Milestone 2: the research CLI (docs/PLAN.md §26).

    uv run vnr-research "what did NVIDIA recently release around agentic models?"

No UI, no ASR — this proves the research architecture on its own: Nemotron via Token
Factory, autonomous Tavily calls, enforced budgets, progress events, cited answer.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import sys
from pathlib import Path
from typing import Any

from .. import logging as vnr_logging
from ..config import Settings
from ..errors import VnrError
from ..events import Event
from ..research.citations import render_cited_sources
from ..research.nebius import NebiusClient
from ..research.runner import run_research

TICK = "✓"
DOT = "●"


class TerminalRenderer:
    """Renders service events as the progress display from PLAN §3 and §18."""

    def __init__(self, *, color: bool = True, show_events: bool = False) -> None:
        from rich.console import Console

        self.console = Console(stderr=True, no_color=not color, highlight=False)
        self.show_events = show_events
        self._answer_started = False

    def __call__(self, event: Event) -> None:
        if self.show_events:
            print(event.to_json(), file=sys.stderr, flush=True)
        handler = getattr(self, f"_on_{event.type.name.lower()}", None)
        if handler is not None:
            handler(event.data)

    # -- handlers ------------------------------------------------------------------
    def _on_research_started(self, data: dict[str, Any]) -> None:
        self.console.print()
        self.console.print(f"[bold]Researching…[/bold] {data.get('query', '')}")
        self.console.print(f"[green]{TICK}[/green] Preparing research")

    def _on_research_search_started(self, data: dict[str, Any]) -> None:
        depth = data.get("search_depth", "basic")
        suffix = f" [dim]({depth})[/dim]" if depth != "basic" else ""
        self.console.print(f'[green]{TICK}[/green] Searching: "{data.get("query", "")}"{suffix}')

    def _on_research_search_completed(self, data: dict[str, Any]) -> None:
        if data.get("error"):
            self.console.print(f"  [red]![/red] Search unavailable ({data['error']})")
            return
        new = len(data.get("new_source_ids") or [])
        count = data.get("result_count", 0)
        detail = f"{count} results" + (f", {new} new sources" if new != count else "")
        self.console.print(f"  [dim]{detail}[/dim]")

    def _on_research_synthesizing(self, data: dict[str, Any]) -> None:
        self.console.print(f"[green]{TICK}[/green] Comparing {data.get('source_count', 0)} sources")
        self.console.print(f"[cyan]{DOT}[/cyan] Writing answer…")
        self.console.print()

    def _on_research_answer_delta(self, data: dict[str, Any]) -> None:
        self._answer_started = True
        sys.stdout.write(str(data.get("text", "")))
        sys.stdout.flush()

    def _on_research_completed(self, data: dict[str, Any]) -> None:
        if self._answer_started:
            sys.stdout.write("\n")
            sys.stdout.flush()
        # Rendered here rather than carried in the answer text: the structured list is
        # the authority, and every layer draws it in its own form.
        sources = render_cited_sources(data.get("cited_sources") or [])
        if sources:
            self.console.print()
            self.console.print(sources)
        metrics = data.get("metrics") or {}
        self.console.print()
        self.console.print(
            "[dim]"
            f"{data.get('turns', 0)} turns · {data.get('searches', 0)} searches · "
            f"{data.get('source_count', 0)} sources · "
            f"{len(data.get('cited_sources') or [])} cited · "
            f"{metrics.get('tavily_credits', 0)} Tavily credits · "
            f"{metrics.get('input_tokens', 0)}in/{metrics.get('output_tokens', 0)}out tokens"
            "[/dim]"
        )
        self.console.print(
            "[dim]"
            f"first search {_ms(metrics.get('go_to_first_search_ms'))} · "
            f"first answer token {_ms(metrics.get('go_to_first_answer_token_ms'))} · "
            f"total {_ms(metrics.get('go_to_completed_ms'))}"
            "[/dim]"
        )
        if data.get("invalid_citation_ids"):
            self.console.print(
                f"[yellow]![/yellow] dropped invalid citations: "
                f"{', '.join(data['invalid_citation_ids'])}"
            )

    def _on_research_failed(self, data: dict[str, Any]) -> None:
        self.console.print(f"\n[red]{data.get('message', 'Research failed')}[/red]")

    def _on_research_cancelled(self, data: dict[str, Any]) -> None:  # noqa: ARG002
        self.console.print("\n[yellow]Cancelled.[/yellow]")


def _ms(value: Any) -> str:
    return "—" if value is None else f"{float(value) / 1000:.2f}s"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vnr-research",
        description="Run one web-research request through Nebius Token Factory + Tavily.",
    )
    parser.add_argument("query", nargs="*", help="the request (or pipe it on stdin)")
    parser.add_argument("--list-models", action="store_true", help="GET /v1/models and exit")
    parser.add_argument("--max-turns", type=int, help="override RESEARCH_MAX_TURNS")
    parser.add_argument("--max-searches", type=int, help="override RESEARCH_MAX_SEARCHES")
    parser.add_argument(
        "--depth", choices=["basic", "advanced"], help="override the default search depth"
    )
    parser.add_argument("--json", action="store_true", help="print the session JSON on stdout")
    parser.add_argument("--save", metavar="DIR", help="write the session JSON into DIR")
    parser.add_argument("--events", action="store_true", help="echo raw events to stderr")
    parser.add_argument("--no-color", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="structured debug logs")
    return parser


def _apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    budget = settings.budget
    changes: dict[str, Any] = {}
    if args.max_turns:
        changes["max_turns"] = args.max_turns
    if args.max_searches:
        changes["max_searches"] = args.max_searches
    if args.depth:
        changes["default_depth"] = args.depth
    if not changes:
        return settings
    return dataclasses.replace(settings, budget=dataclasses.replace(budget, **changes))


async def _run(args: argparse.Namespace) -> int:
    settings = _apply_overrides(Settings.load(), args)

    if args.list_models:
        async with NebiusClient(settings.nebius) as client:
            models = await client.list_models()
        for model in sorted(models):
            marker = " <- configured" if model == settings.nebius.model else ""
            print(f"{model}{marker}")
        if settings.nebius.model not in models:
            print(
                f"\nWARNING: configured NEBIUS_MODEL {settings.nebius.model!r} is not in the "
                "list above.",
                file=sys.stderr,
            )
            return 1
        return 0

    query = " ".join(args.query).strip()
    if not query and not sys.stdin.isatty():
        query = sys.stdin.read().strip()
    if not query:
        print("No query given. Pass it as an argument or pipe it on stdin.", file=sys.stderr)
        return 2

    renderer = TerminalRenderer(color=not args.no_color, show_events=args.events)
    try:
        session, _result = await run_research(query, settings, sink=renderer)
    except VnrError:
        # The renderer already showed the message on research.failed; all that is left is
        # to hand the query back, so nothing the user approved is lost (PLAN §22).
        print(f"Your request is unchanged: {query!r}", file=sys.stderr)
        return 1
    except asyncio.CancelledError:
        return 130

    if args.save:
        path = session.save(Path(args.save))
        print(f"session saved to {path}", file=sys.stderr)
    if args.json:
        print(json.dumps(session.to_dict(), ensure_ascii=False, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Structured logs are a debugging tool; the renderer is what talks to the user.
    vnr_logging.configure(logging.DEBUG if args.verbose else logging.CRITICAL)
    try:
        return asyncio.run(_run(args))
    except VnrError as exc:
        # Config and provider failures outside a research run (e.g. --list-models).
        print(exc.user_message, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
