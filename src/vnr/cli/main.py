"""Terminal entry point. Import hardware dependencies only for voice commands."""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import sys
from pathlib import Path

from ..config import Settings
from ..errors import VnrError


def doctor(*, text_only: bool = False) -> int:
    """Local preflight: no API calls, microphone capture or model allocation."""
    settings = Settings.load()
    checks = [
        ("NEBIUS_API_KEY configured", bool(settings.nebius.api_key)),
        ("TAVILY_API_KEY configured", bool(settings.tavily.api_key)),
    ]
    if not text_only:
        checks.append(("Apple Silicon macOS", platform.system() == "Darwin"
                       and platform.machine() == "arm64"))
        for module in ("sounddevice", "fastapi", "uvicorn", "websockets", "prompt_toolkit",
                       "moshi_mlx", "mlx", "rustymimi", "sentencepiece"):
            checks.append((f"{module} installed", importlib.util.find_spec(module) is not None))
    for label, ok in checks:
        print(f"{'OK' if ok else 'MISSING'}  {label}")
    print(f"Research model: {settings.nebius.model}")
    if not text_only:
        print(f"ASR: {settings.asr.engine} / {settings.asr.hf_repo}")
        print(f"Load-time quantization: {settings.asr.quant_bits or 'checkpoint default'}")
        print(f"Metal cache cap: {settings.asr.cache_limit_mb} MiB; "
              f"idle unload: {settings.asr.idle_timeout_s:g}s")
        print("First voice use downloads weights if absent. Wait for Listening before speaking.")
    print("Local checks only: credentials, network, microphone permission and model quality "
          "are not validated.")
    if not all(ok for _, ok in checks):
        print('Setup: uv pip install -e ".[asr,service,mlx]"; fill keys in .env. '
              'Text-only use: vnr doctor --text')
        return 1
    return 0


def inspect_session(
    path: str, *, reasoning: bool = False, all_sources: bool = False
) -> int:
    from rich.console import Console
    from rich.markdown import Markdown

    data = json.loads(Path(path).read_text())
    console = Console(highlight=False)
    console.print("Approved request: " + str(data.get("submitted_query", "")), markup=False)
    console.print(Markdown(str(data.get("answer", ""))))
    console.print("\nAnswer citations:", style="bold")
    for source in data.get("cited_sources", []):
        console.print(f"[{source['number']}] → {source['id']} — {source['url']}", markup=False)
    console.print("\nRetrieved evidence (source IDs refer to retrieval order):", style="bold")
    evidence = (data.get("sources", []) if all_sources else
                data.get("cited_sources") or data.get("sources", []))
    for source in evidence:
        console.print(f"[{source['id']}] {source['title']}\n{source['url']}", markup=False)
        console.print(str(source.get("content", "")), markup=False)
        if source.get("published_date"):
            console.print("Published: " + str(source["published_date"]), markup=False)
    console.print("\nSearches:", style="bold")
    for search in data.get("searches", []):
        console.print(f"{search['query']} — {search.get('result_count', 0)} results", markup=False)
    console.print("\nCitation membership checks do not verify factual accuracy. "
                  "Open the sources and compare each claim, date and timezone.")
    if reasoning:
        console.print("\nModel reasoning (not evidence):", style="bold")
        console.print(str(data.get("reasoning", "")), markup=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(prog="vnr", description="Local speech → review → web research")
    parser.add_argument("command", choices=["doctor", "serve", "voice", "ask", "inspect"])
    if not argv or argv[0] in {"-h", "--help"}:
        parser.print_help()
        print("\nStart: vnr doctor · vnr serve · vnr voice\n"
              'Text: vnr ask --save runs "your question"\n'
              "Evidence: vnr inspect runs/SESSION.json [--reasoning]\n"
              "Use vnr COMMAND --help for options.")
        return 0
    command = parser.parse_args(argv[:1]).command
    rest = argv[1:]
    try:
        if command == "doctor":
            sub = argparse.ArgumentParser(prog="vnr doctor")
            sub.add_argument("--text", action="store_true", help="check text research only")
            return doctor(text_only=sub.parse_args(rest).text)
        if command == "inspect":
            sub = argparse.ArgumentParser(prog="vnr inspect")
            sub.add_argument("session", help="JSON from vnr ask --save")
            sub.add_argument("--reasoning", action="store_true")
            sub.add_argument("--all-sources", action="store_true",
                             help="include retrieved sources not cited in the answer")
            args = sub.parse_args(rest)
            return inspect_session(args.session, reasoning=args.reasoning,
                                   all_sources=args.all_sources)
        if command == "ask":
            from .research_cli import main as run
        elif command == "serve":
            from ..service import main as run
        else:
            from .prototype import main as run
        return run(rest)
    except ImportError:
        print('Missing dependency. Run: uv pip install -e ".[asr,service,mlx]"', file=sys.stderr)
        return 1
    except VnrError as exc:
        print(exc.user_message, file=sys.stderr)
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Unable to complete {command}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
