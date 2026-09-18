"""Milestone 3: the end-to-end local prototype (docs/PLAN.md §26).

    mic → quantized ASR → live transcript → edit → GO → research → streamed answer

A throwaway terminal UI that talks to the local service over the same loopback WebSocket
the SwiftUI app will use, and captures audio the same way the app will. So this is not a
mock of the data path — it *is* the data path, with a terminal where the overlay goes.

    uv run vnr-service          # terminal 1
    uv run vnr-prototype        # terminal 2
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
from typing import Any

from .. import logging as vnr_logging
from ..asr.audio import MicrophoneSource
from ..config import Settings
from ..events import EventType

TICK = "✓"
DOT = "●"


class Renderer:
    """Turns service events into the display from PLAN §3."""

    def __init__(self, console: Any) -> None:
        self.console = console
        self.transcript = ""
        self.answer_started = False
        self.finished = asyncio.Event()
        self.final_seen = asyncio.Event()
        self.failed: str | None = None
        self._live: Any = None

    def attach_live(self, live: Any) -> None:
        self._live = live

    def handle(self, event: dict[str, Any]) -> None:
        data = event.get("data") or {}
        match event.get("type"):
            case EventType.ASR_PARTIAL.value:
                self.transcript = str(data.get("text", ""))
                self._show_transcript()
            case EventType.ASR_FINAL.value:
                self.transcript = str(data.get("text", ""))
                self._show_transcript()
                self.final_seen.set()
            case EventType.ASR_ERROR.value:
                self.failed = str(data.get("message", "Speech recognition failed."))
                self.console.print(f"[red]{self.failed}[/red]")
                self.final_seen.set()
                self.finished.set()
            case EventType.RESEARCH_STARTED.value:
                self.console.print(f"\n[green]{TICK}[/green] Preparing research")
            case EventType.RESEARCH_SEARCH_STARTED.value:
                self.console.print(f'[green]{TICK}[/green] Searching: "{data.get("query", "")}"')
            case EventType.RESEARCH_SEARCH_COMPLETED.value:
                if data.get("error"):
                    self.console.print("  [red]Search unavailable[/red]")
                else:
                    self.console.print(f"  [dim]{data.get('result_count', 0)} results[/dim]")
            case EventType.RESEARCH_SYNTHESIZING.value:
                self.console.print(
                    f"[green]{TICK}[/green] Comparing {data.get('source_count', 0)} sources"
                )
                self.console.print(f"[cyan]{DOT}[/cyan] Writing answer…\n")
            case EventType.RESEARCH_ANSWER_DELTA.value:
                self.answer_started = True
                sys.stdout.write(str(data.get("text", "")))
                sys.stdout.flush()
            case EventType.RESEARCH_COMPLETED.value:
                self._end_answer()
                metrics = data.get("metrics") or {}
                self.console.print(
                    f"[dim]{data.get('turns', 0)} turns · {data.get('searches', 0)} searches · "
                    f"{metrics.get('tavily_credits', 0)} credits[/dim]"
                )
                self.finished.set()
            case EventType.RESEARCH_FAILED.value:
                self._end_answer()
                self.failed = str(data.get("message", "Research failed."))
                self.console.print(f"[red]{self.failed}[/red]")
                self.finished.set()
            case EventType.RESEARCH_CANCELLED.value:
                self._end_answer()
                self.console.print("[yellow]Cancelled.[/yellow]")
                self.finished.set()

    def _end_answer(self) -> None:
        if self.answer_started:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.answer_started = False

    def _show_transcript(self) -> None:
        if self._live is not None:
            from rich.text import Text

            self._live.update(Text(self.transcript or "…", style="bold"))


async def edit_transcript(text: str) -> str | None:
    """Show the transcript in a genuinely editable line (docs/PLAN.md §7).

    ``readline.set_startup_hook`` is not usable here: on macOS the stdlib ``readline`` is
    linked against libedit, which ignores the hook while still importing cleanly — so the
    line came up empty and Enter silently cancelled. prompt_toolkit prefills for real.

    Semantics follow §7 exactly: edit in place, Enter approves whatever is in the line,
    and clearing it cancels. Approval is never the default keypress on an empty line.
    """
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings
    except ImportError:
        return await asyncio.to_thread(_edit_transcript_fallback, text)

    bindings = KeyBindings()

    @bindings.add("c-c")
    def _cancel(event) -> None:  # Ctrl-C leaves with nothing, like an emptied line
        event.app.exit(result="")

    try:
        session: PromptSession[str] = PromptSession(key_bindings=bindings)
        # prompt_async, not prompt: this runs on the loop we are already on. Driving
        # prompt_toolkit from a worker thread can fail setting up signal handlers.
        return await session.prompt_async("GO > ", default=text)
    except (EOFError, KeyboardInterrupt):
        return None
    except Exception:
        # No usable terminal (piped stdin, odd TERM). Don't lose the transcript.
        return await asyncio.to_thread(_edit_transcript_fallback, text)


def _edit_transcript_fallback(text: str) -> str | None:
    """Stdlib path for terminals prompt_toolkit cannot drive. Never prefills silently."""
    print(f"\nTranscript: {text}")
    print("[Enter] research as-is · [e] edit · [c] cancel")
    try:
        choice = input("> ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None
    if choice == "c":
        return None
    if choice == "e":
        try:
            return input("edited > ")
        except (EOFError, KeyboardInterrupt):
            return None
    return text


def approved_query(answer: str | None) -> str | None:
    """What GO actually sends, or None to cancel (docs/PLAN.md §7).

    Cancelling (Ctrl-C, or clearing the line) and approving whitespace are the same thing:
    nothing is sent. This is the decision the libedit bug got wrong for every run.
    """
    if answer is None:
        return None
    return answer.strip() or None


async def _receive(websocket: Any, renderer: Renderer) -> None:
    async for raw in websocket:
        if isinstance(raw, bytes):
            continue
        with contextlib.suppress(json.JSONDecodeError):
            renderer.handle(json.loads(raw))


async def _record(websocket: Any, settings: Settings, stop: asyncio.Event) -> None:
    source = MicrophoneSource(settings.asr)
    async for frame in source.frames():
        if stop.is_set():
            return
        await websocket.send(frame)


async def _run(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.live import Live
    from websockets.asyncio.client import connect

    console = Console(stderr=True, highlight=False)
    settings = Settings.load()
    url = f"ws://{args.host or settings.service.host}:{args.port or settings.service.port}/ws"

    try:
        websocket = await connect(url, max_size=None)
    except OSError:
        console.print(f"[red]No service at {url}. Start it with:[/red] uv run vnr-service")
        return 1

    renderer = Renderer(console)
    async with websocket:
        receiver = asyncio.create_task(_receive(websocket, renderer))
        try:
            await websocket.send(json.dumps({"type": "recording.start"}))
            console.print("[bold]Listening…[/bold] press Enter when you're done speaking")

            stop = asyncio.Event()
            with Live(console=console, refresh_per_second=12) as live:
                renderer.attach_live(live)
                recorder = asyncio.create_task(_record(websocket, settings, stop))
                await asyncio.to_thread(sys.stdin.readline)
                stop.set()
                recorder.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await recorder
                await websocket.send(json.dumps({"type": "recording.stop"}))
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(renderer.final_seen.wait(), timeout=15)
            renderer.attach_live(None)

            if renderer.failed:
                return 1

            console.print("\n[bold]Review — edit if the transcript is wrong, "
                          "Enter to research, empty to cancel[/bold]")
            approved = approved_query(await edit_transcript(renderer.transcript))
            if approved is None:
                console.print("[yellow]Cancelled — nothing was sent.[/yellow]")
                return 0

            await websocket.send(json.dumps({"type": "research.submit", "query": approved}))
            try:
                await renderer.finished.wait()
            except asyncio.CancelledError:
                await websocket.send(json.dumps({"type": "research.cancel"}))
                raise
            return 1 if renderer.failed else 0
        finally:
            receiver.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await receiver


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="vnr-prototype", description="Milestone 3: mic → transcript → GO → cited answer."
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    vnr_logging.configure(logging.DEBUG if args.verbose else logging.CRITICAL)
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
