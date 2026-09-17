"""Local streaming speech recognition (docs/PLAN.md §5, §6)."""

from .engine import AsrEngine, Transcript
from .registry import create_engine

__all__ = ["AsrEngine", "Transcript", "create_engine"]
