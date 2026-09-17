"""Milestone 1: the quantized streaming ASR spike (docs/PLAN.md §5, §26).

    uv run vnr-asr-spike

    microphone → quantized Kyutai STT → continuously updating transcript

Nothing else. This is the hard gate: it exists to answer whether the Q4_K runtime
streams acceptably on Apple Silicon, and to measure it. The numbers it prints are the
ones §5 asks for — model load time, resident memory, real-time factor, audio→transcript
delay, and stability over a long run.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import time
from typing import Any

from .. import logging as vnr_logging
from ..asr.audio import AudioError, MicrophoneSource, SilenceSource, WavFileSource
from ..asr.engine import AsrEngine
from ..asr.registry import create_engine
from ..config import AsrConfig, Settings
from ..errors import VnrError
from ..events import Event, EventEmitter, EventType
from ..metrics import AsrMetrics, peak_rss_mb, process_rss_mb


class SpikeRecorder:
    """Times the ASR path and drives the live transcript display."""

    def __init__(self, *, live: Any = None) -> None:
        self.metrics = AsrMetrics()
        self.transcript = ""
        self.first_audio_at: float | None = None
        self.stopped_at: float | None = None
        self._live = live

    def note_first_audio(self) -> None:
        if self.first_audio_at is None:
            self.first_audio_at = time.monotonic()

    def __call__(self, event: Event) -> None:
        if event.type is EventType.ASR_PARTIAL:
            self.metrics.partial_count += 1
            if self.metrics.first_partial_ms is None and self.first_audio_at is not None:
                self.metrics.first_partial_ms = (time.monotonic() - self.first_audio_at) * 1000
            self._update(str(event.data.get("text", "")))
        elif event.type is EventType.ASR_FINAL:
            if self.stopped_at is not None:
                self.metrics.finalize_ms = (time.monotonic() - self.stopped_at) * 1000
            self._update(str(event.data.get("text", "")))

    def _update(self, text: str) -> None:
        self.transcript = text
        if self._live is not None:
            from rich.text import Text

            self._live.update(Text(text or "…", style="bold"))


async def _wait_for_enter() -> None:
    with contextlib.suppress(Exception):
        await asyncio.to_thread(sys.stdin.readline)


class RssSampler:
    """Polls a process's resident set so we can report the peak (PLAN §5)."""

    def __init__(self, pid: int | None, interval_s: float = 0.5) -> None:
        self.pid = pid
        self.interval_s = interval_s
        self.peak_mb: float | None = None
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self.pid is not None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _run(self) -> None:
        while True:
            rss = process_rss_mb(self.pid)
            if rss is not None and (self.peak_mb is None or rss > self.peak_mb):
                self.peak_mb = rss
            await asyncio.sleep(self.interval_s)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vnr-asr-spike",
        description="Milestone 1: prove the quantized streaming ASR runtime locally.",
    )
    parser.add_argument("--engine", choices=["moshicpp", "mock"], help="override VNR_ASR_ENGINE")
    parser.add_argument("--seconds", type=float, help="stop automatically after N seconds")
    parser.add_argument("--file", metavar="WAV", help="replay a 16-bit mono WAV instead of the mic")
    parser.add_argument(
        "--realtime", action="store_true", help="with --file, pace playback like a live mic"
    )
    parser.add_argument("--device", help="input device index or name")
    parser.add_argument("--silence", type=float, metavar="SECONDS", help="harness self-test input")
    parser.add_argument("--print-command", action="store_true", help="show the argv and exit")
    parser.add_argument("--json", action="store_true", help="emit the metrics as JSON")
    parser.add_argument("--verbose", action="store_true")
    return parser


def _make_source(args: argparse.Namespace, config: AsrConfig):
    if args.file:
        return WavFileSource(args.file, config, realtime=args.realtime)
    if args.silence:
        return SilenceSource(config, seconds=args.silence)
    device: int | str | None = args.device
    if isinstance(device, str) and device.isdigit():
        device = int(device)
    return MicrophoneSource(config, device=device)


async def _stream(engine: AsrEngine, source: Any, recorder: SpikeRecorder, stop: asyncio.Event):
    async for frame in source.frames():
        if stop.is_set():
            break
        recorder.note_first_audio()
        await engine.push_audio(frame)


async def _run(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.live import Live

    console = Console(stderr=True, highlight=False)
    settings = Settings.load()
    config = settings.asr
    if args.engine:
        config = AsrConfig(**{**config.__dict__, "engine": args.engine})

    engine = create_engine(config)

    if args.print_command:
        from ..asr.moshicpp import MoshiCppEngine

        if isinstance(engine, MoshiCppEngine):
            console.print(" ".join(engine.command()))
            return 0
        console.print(f"engine {engine.name!r} runs in-process — there is no command")
        return 0

    if engine.name == "mock":
        console.print(
            "[yellow]Running the MOCK engine — it invents text and proves nothing about "
            "Milestone 1.[/yellow]"
        )
    else:
        from ..asr.moshicpp import MoshiCppEngine

        if isinstance(engine, MoshiCppEngine):
            console.print(f"[dim]$ {' '.join(engine.command())}[/dim]")

    console.print("Loading model…", end="")
    load_started = time.monotonic()
    await engine.load()
    load_ms = (time.monotonic() - load_started) * 1000
    console.print(f"\rModel resident in {load_ms / 1000:.2f}s. ")
    if engine.name != "mock" and not config.ready_marker:
        console.print(
            f"[dim]No VNR_ASR_READY_MARKER set, so that figure includes a "
            f"{config.ready_probe_s:g}s startup probe and is only a lower bound. Set the "
            f"marker to whatever your binary prints when the weights are in.[/dim]"
        )

    source = _make_source(args, config)
    sampler = RssSampler(getattr(getattr(engine, "_process", None), "pid", None))
    stop = asyncio.Event()

    if args.seconds:
        console.print(f"[bold]Listening…[/bold] (stopping after {args.seconds:g}s)")
    elif args.file or args.silence:
        console.print("[bold]Replaying audio…[/bold]")
    else:
        console.print("[bold]Listening…[/bold] press Enter to stop")

    with Live(console=console, refresh_per_second=12, transient=False) as live:
        recorder = SpikeRecorder(live=live)
        recorder.metrics.model_load_ms = load_ms
        await engine.start_session(EventEmitter(recorder, session_id="spike"))
        sampler.start()

        stream_task = asyncio.create_task(_stream(engine, source, recorder, stop))
        waiters: list[asyncio.Task[Any]] = [stream_task]
        if args.seconds:
            waiters.append(asyncio.create_task(asyncio.sleep(args.seconds)))
        elif not (args.file or args.silence):
            waiters.append(asyncio.create_task(_wait_for_enter()))

        started = time.monotonic()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
        stop.set()
        for task in waiters:
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.gather(*waiters, return_exceptions=True)

        recorder.stopped_at = time.monotonic()
        recorder.metrics.recording_ms = (recorder.stopped_at - started) * 1000
        final = await engine.finalize_session()

    await sampler.stop()
    metrics = recorder.metrics
    metrics.audio_seconds = getattr(source, "seconds_captured", 0.0)
    replayed_fast = bool(args.file) and not args.realtime
    metrics.decode_seconds = (time.monotonic() - started) if replayed_fast else 0.0
    metrics.peak_rss_mb = sampler.peak_mb or peak_rss_mb()
    await engine.unload()

    console.print()
    console.print("[bold]Transcript[/bold]")
    console.print(final or "[dim](empty)[/dim]")
    console.print()
    _report(console, engine.name, metrics, realtime_source=not args.file or args.realtime)
    if args.json:
        print(json.dumps({"engine": engine.name, **metrics.to_dict()}, indent=2))
    return 0


def _report(console: Any, engine_name: str, metrics: AsrMetrics, *, realtime_source: bool) -> None:
    from rich.table import Table

    table = Table(title=f"Milestone 1 — {engine_name}", title_justify="left", show_header=False)
    table.add_column("metric", style="dim")
    table.add_column("value")
    rows = [
        ("model load", _s(metrics.model_load_ms)),
        ("audio captured", f"{metrics.audio_seconds:.1f}s"),
        ("audio → first transcript", _s(metrics.first_partial_ms)),
        ("recording end → final", _s(metrics.finalize_ms)),
        ("transcript updates", str(metrics.partial_count)),
        (
            "peak resident memory",
            "—" if metrics.peak_rss_mb is None else f"{metrics.peak_rss_mb:.0f} MB",
        ),
    ]
    rtf = metrics.real_time_factor
    if realtime_source:
        rows.append(("real-time factor", "n/a (live source runs at 1.0x by definition)"))
    else:
        rows.append(("real-time factor", "—" if rtf is None else f"{rtf:.2f}x"))
    for name, value in rows:
        table.add_row(name, value)
    console.print(table)


def _s(ms: float | None) -> str:
    return "—" if ms is None else f"{ms / 1000:.2f}s"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    vnr_logging.configure(logging.DEBUG if args.verbose else logging.CRITICAL)
    try:
        return asyncio.run(_run(args))
    except (VnrError, AudioError) as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
