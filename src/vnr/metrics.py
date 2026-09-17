"""Latency / token / credit instrumentation (docs/PLAN.md §21).

Structured numbers, not an observability platform. Every duration is milliseconds measured
on a monotonic clock, relative to the moment the user pressed GO (or started recording).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Clock = Callable[[], float]


@dataclass
class Timeline:
    """Named marks relative to a start instant."""

    clock: Clock = time.monotonic
    started_at: float = field(default=0.0)
    marks: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.started_at:
            self.started_at = self.clock()

    def mark(self, name: str, *, once: bool = True) -> float:
        """Record *name*. With ``once`` (the default) the first value wins."""
        elapsed_ms = (self.clock() - self.started_at) * 1000.0
        if once and name in self.marks:
            return self.marks[name]
        self.marks[name] = elapsed_ms
        return elapsed_ms

    def get(self, name: str) -> float | None:
        return self.marks.get(name)

    def elapsed_ms(self) -> float:
        return (self.clock() - self.started_at) * 1000.0


@dataclass
class ResearchMetrics:
    """Agent-side measurements, all relative to GO."""

    timeline: Timeline = field(default_factory=Timeline)
    turns: int = 0
    searches: int = 0
    advanced_searches: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    tavily_credits: int = 0
    search_latencies_ms: list[float] = field(default_factory=list)
    cost_usd: float | None = None

    # Mark names kept as constants so the CLI and tests can't drift from the agent.
    FIRST_MODEL_RESPONSE = "first_model_response"
    FIRST_SEARCH = "first_search"
    SYNTHESIS_STARTED = "synthesis_started"
    FIRST_ANSWER_TOKEN = "first_answer_token"
    COMPLETED = "completed"

    def mark(self, name: str, *, once: bool = True) -> float:
        return self.timeline.mark(name, once=once)

    def record_usage(self, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        self.input_tokens += int(usage.get("prompt_tokens") or 0)
        self.output_tokens += int(usage.get("completion_tokens") or 0)
        cost = usage.get("cost") or usage.get("total_cost")
        if cost is not None:
            self.cost_usd = (self.cost_usd or 0.0) + float(cost)

    def record_search(self, *, latency_ms: float, credits: int, advanced: bool) -> None:
        self.searches += 1
        self.search_latencies_ms.append(latency_ms)
        self.tavily_credits += credits
        if advanced:
            self.advanced_searches += 1

    def to_dict(self) -> dict[str, Any]:
        m = self.timeline.marks
        return {
            "turns": self.turns,
            "searches": self.searches,
            "advanced_searches": self.advanced_searches,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tavily_credits": self.tavily_credits,
            "cost_usd": self.cost_usd,
            "go_to_first_model_response_ms": _round(m.get(self.FIRST_MODEL_RESPONSE)),
            "go_to_first_search_ms": _round(m.get(self.FIRST_SEARCH)),
            "go_to_synthesis_ms": _round(m.get(self.SYNTHESIS_STARTED)),
            "go_to_first_answer_token_ms": _round(m.get(self.FIRST_ANSWER_TOKEN)),
            "go_to_completed_ms": _round(m.get(self.COMPLETED)),
            "search_latencies_ms": [_round(v) for v in self.search_latencies_ms],
        }


@dataclass
class AsrMetrics:
    """Milestone 1 measurements (PLAN §5, §21)."""

    model_load_ms: float | None = None
    recording_ms: float | None = None
    audio_seconds: float = 0.0
    decode_seconds: float = 0.0
    first_partial_ms: float | None = None
    finalize_ms: float | None = None
    peak_rss_mb: float | None = None
    partial_count: int = 0

    @property
    def real_time_factor(self) -> float | None:
        """decode wall-time ÷ audio duration. < 1.0 means faster than real time."""
        if self.audio_seconds <= 0 or self.decode_seconds <= 0:
            return None
        return self.decode_seconds / self.audio_seconds

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_load_ms": _round(self.model_load_ms),
            "recording_ms": _round(self.recording_ms),
            "audio_seconds": _round(self.audio_seconds, 3),
            "first_partial_ms": _round(self.first_partial_ms),
            "recording_end_to_final_ms": _round(self.finalize_ms),
            "partial_count": self.partial_count,
            "peak_rss_mb": _round(self.peak_rss_mb, 1),
            "real_time_factor": _round(self.real_time_factor, 3),
        }


def _round(value: float | None, digits: int = 1) -> float | None:
    return None if value is None else round(value, digits)


def peak_rss_mb() -> float | None:
    """Best-effort resident-set peak for this process."""
    try:
        import resource
    except ImportError:  # pragma: no cover - non-POSIX
        return None
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS reports bytes, Linux reports kilobytes.
    import sys

    return usage / (1024 * 1024) if sys.platform == "darwin" else usage / 1024


def process_rss_mb(pid: int | None) -> float | None:
    """Resident set of another process, in MB. Used to measure the ASR child process."""
    if pid is None:
        return None
    import subprocess

    try:
        out = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = out.stdout.strip()
    return int(value) / 1024 if value.isdigit() else None  # ps reports kilobytes
