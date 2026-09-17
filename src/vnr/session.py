"""The session object the UI and the service share (docs/PLAN.md §20).

In-memory, with optional JSON persistence. No database in V1.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .events import SessionState
from .metrics import AsrMetrics, ResearchMetrics
from .research.sources import SearchRecord, Source


@dataclass
class ResearchSession:
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created_at: float = field(default_factory=time.time)

    #: Exactly what the ASR produced …
    raw_transcript: str = ""
    #: … and exactly what the user approved. They differ when the user edits (PLAN §7).
    submitted_query: str = ""

    status: SessionState = SessionState.IDLE
    searches: list[SearchRecord] = field(default_factory=list)
    sources: list[Source] = field(default_factory=list)
    answer: str = ""

    asr_metrics: AsrMetrics = field(default_factory=AsrMetrics)
    research_metrics: ResearchMetrics = field(default_factory=ResearchMetrics)
    error: str | None = None

    @property
    def transcript_was_edited(self) -> bool:
        """The ASR-quality signal the plan asks us to keep (PLAN §7)."""
        if not self.raw_transcript:
            return False
        return self.raw_transcript.strip() != self.submitted_query.strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "created_at": self.created_at,
            "raw_transcript": self.raw_transcript,
            "submitted_query": self.submitted_query,
            "transcript_was_edited": self.transcript_was_edited,
            "status": self.status.value,
            "searches": [s.to_dict() for s in self.searches],
            "sources": [s.to_dict() for s in self.sources],
            "answer": self.answer,
            "metrics": {
                "asr": self.asr_metrics.to_dict(),
                "research": self.research_metrics.to_dict(),
            },
            "error": self.error,
        }

    def save(self, directory: Path | str) -> Path:
        """Write the session as one JSON file. Optional; nothing depends on it."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{int(self.created_at)}-{self.id}.json"
        path.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2))
        return path
