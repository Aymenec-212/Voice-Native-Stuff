"""Interactive terminal voice client (docs/PLAN.md §26).

    mic → quantized ASR → live transcript → edit → GO → research → streamed answer

Uses the same local service and explicit transcript approval as the native app.
Weights remain warm across questions; disconnect resets only the current session.
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
    """One recording/research session; reasoning is retained for explicit disclosure."""

    def __init__(self, console: Any) -> None:
        from .research_cli import TerminalRenderer

        self.console = console
        self.research = TerminalRenderer()
        self.transcript = ""
        self.reasoning = ""
        self.finished = asyncio.Event()
        self.final_seen = asyncio.Event()
        self.listening = asyncio.Event()
        self.failed: str | None = None
        self._live: Any = None

    def attach_live(self, live: Any) -> None:
        self._live = live

    def handle(self, event: dict[str, Any]) -> None:
        from ..events import Event

        data = event.get("data") or {}
        kind = event.get("type")
        if kind == EventType.STATE_CHANGED.value and data.get("state") == "LISTENING":
            self.listening.set()
        elif kind in {EventType.ASR_PARTIAL.value, EventType.ASR_FINAL.value}:
            self.transcript = str(data.get("text", ""))
            if self._live is not None:
                from rich.text import Text

                self._live.update(Text(self.transcript or "…", style="bold"))
            if kind == EventType.ASR_FINAL.value:
                self.final_seen.set()
        elif kind == EventType.ASR_ERROR.value:
            self.failed = str(data.get("message", "Speech recognition failed."))
            self.console.print(self.failed, style="red", markup=False)
            self.finished.set()
            self.final_seen.set()
            self.listening.set()
        elif kind == EventType.RESEARCH_REASONING_DELTA.value:
            self.reasoning += str(data.get("text", ""))
        elif str(kind).startswith("research."):
            with contextlib.suppress(ValueError):
                self.research(Event(type=EventType(kind), data=data))
            if kind == EventType.RESEARCH_FAILED.value:
                self.failed = str(data.get("message", "Research failed."))
            if kind in {EventType.RESEARCH_COMPLETED.value, EventType.RESEARCH_FAILED.value,
                        EventType.RESEARCH_CANCELLED.value}:
                self.finished.set()


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
    return text if choice == "" else None


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
            event = json.loads(raw)
            if isinstance(event, dict) and isinstance(event.get("data", {}), dict):
                renderer.handle(event)
                if event.get("type") == EventType.ASR_ERROR.value:
                    raise ConnectionError(renderer.failed)
    raise ConnectionError("Service disconnected. Restart it with vnr serve.")


async def _record(websocket: Any, settings: Settings, stop: asyncio.Event) -> None:
    source = MicrophoneSource(settings.asr)
    async for frame in source.frames():
        if stop.is_set():
            return
        await websocket.send(frame)


async def _guarded(awaitable: Any, *watchers: asyncio.Task, timeout: float | None = None):
    """Do not leave a prompt or event wait stranded when transport/capture fails."""
    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait([task, *watchers], timeout=timeout,
                                     return_when=asyncio.FIRST_COMPLETED)
        if not done:
            raise TimeoutError("Service response timed out. Please retry.")
        for watcher in watchers:
            if watcher in done:
                watcher.result()
                raise ConnectionError("Audio capture or service stopped unexpectedly.")
        return task.result()
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _prompt(message: str) -> str:
    from prompt_toolkit import PromptSession

    return await PromptSession().prompt_async(message)


def service_url(host: str, port: int) -> str:
    if host not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Audio must stay local: use localhost, 127.0.0.1 or ::1.")
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return f"ws://{'[' + host + ']' if ':' in host else host}:{port}/ws"


async def _run(args: argparse.Namespace) -> int:
    import httpx
    from rich.console import Console
    from rich.live import Live
    from websockets.asyncio.client import connect

    console = Console(stderr=True, highlight=False)
    settings = Settings.load()
    url = service_url(args.host or settings.service.host, args.port or settings.service.port)
    if not sys.stdin.isatty():
        console.print('Voice needs an interactive terminal. Use vnr ask "question" for text.')
        return 2

    while True:
        console.print("Preparing local speech recognition… first use may download/load weights.")
        async with httpx.AsyncClient(timeout=180, trust_env=False) as http:
            response = await http.post(
                url.replace("ws://", "http://").removesuffix("/ws") + "/asr/prepare"
            )
            response.raise_for_status()
            if not response.json().get("ready"):
                console.print("Speech recognition could not load. Check the vnr serve terminal.")
                return 1

        renderer = Renderer(console)
        # Closing the socket resets the service session; the model remains warm.
        async with connect(url, max_size=None, proxy=None) as websocket:
            receiver = asyncio.create_task(_receive(websocket, renderer))
            try:
                await websocket.send(json.dumps({"type": "recording.start"}))
                await _guarded(renderer.listening.wait(), receiver, timeout=15)
                if renderer.failed:
                    return 1
                console.print("[bold]Listening…[/bold] speak now; press Enter to stop.")
                stop = asyncio.Event()
                with Live(console=console, refresh_per_second=12) as live:
                    renderer.attach_live(live)
                    recorder = asyncio.create_task(_record(websocket, settings, stop))
                    try:
                        await _guarded(_prompt(""), receiver, recorder)
                    finally:
                        stop.set()
                        recorder.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await recorder
                    await websocket.send(json.dumps({"type": "recording.stop"}))
                    await _guarded(renderer.final_seen.wait(), receiver, timeout=60)
                renderer.attach_live(None)
                if renderer.failed:
                    return 1
                console.print("Review/edit the transcript. Enter sends this text to research; "
                              "clear the line to cancel.")
                approved = approved_query(await _guarded(
                    edit_transcript(renderer.transcript), receiver))
                if approved is not None:
                    await websocket.send(json.dumps({"type": "research.submit", "query": approved}))
                    await _guarded(renderer.finished.wait(), receiver, timeout=600)
                else:
                    console.print("Cancelled — nothing sent for research.")
            finally:
                renderer.research.close()
                receiver.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await receiver
        if args.once:
            return 1 if renderer.failed else 0
        while True:
            choice = await _prompt("Enter: record again · t: model trace · q: quit > ")
            choice = choice.strip().lower()
            if choice == "q":
                return 0
            if choice == "t":
                console.print(renderer.reasoning or "No model reasoning received.", markup=False)
            elif choice == "":
                break


def main(argv: list[str] | None = None) -> int:
    from ..errors import VnrError

    parser = argparse.ArgumentParser(
        prog="vnr voice",
        description="Speak → review/edit → GO → cited answer; repeat without restart."
    )
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--once", action="store_true", help="exit after one question")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)
    vnr_logging.configure(logging.DEBUG if args.verbose else logging.CRITICAL)
    try:
        return asyncio.run(_run(args))
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled.", file=sys.stderr)
        return 130
    except VnrError as exc:
        print(exc.user_message, file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Voice session stopped: {exc}\nCheck vnr serve and run vnr doctor.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
