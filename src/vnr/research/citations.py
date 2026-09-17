"""Citation rewriting and validation (docs/PLAN.md §13).

The model cites retrieved sources by their session ID — ``[S3]``. The *application*
renumbers those to reader-facing ``[1] [2] …`` in order of first appearance and builds the
Sources list from URLs Tavily actually returned. The model never writes a URL, so it
cannot invent one. IDs that don't exist in the registry are dropped and reported.

:class:`CitationRewriter` is streaming-safe: a citation split across two deltas
(``"…see [S"`` then ``"12] and"``) is held back until it can be resolved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .sources import Source, SourceRegistry

#: One or more IDs in a single bracket: [S1] or [S1, S2]. Any single space in front is
#: captured too, so dropping an invalid citation doesn't leave "the claim ." behind.
CITATION_RE = re.compile(r"([ \t]?)\[\s*(S\d+(?:\s*,\s*S\d+)*)\s*\]", re.IGNORECASE)
#: A trailing fragment that might still grow into a citation — including that space, so the
#: rule still works when a citation straddles two stream deltas.
#: A lone trailing space is held back too — the citation it precedes may arrive in the
#: next delta, and by then it is too late to take the space back.
PARTIAL_RE = re.compile(r"(?:[ \t]?\[[\sS\d,]*|[ \t])$", re.IGNORECASE)


@dataclass
class CitationReport:
    cited: list[Source] = field(default_factory=list)
    invalid_ids: list[str] = field(default_factory=list)

    @property
    def uncited(self) -> bool:
        return not self.cited

    def to_dict(self) -> dict[str, object]:
        return {
            "cited_source_ids": [s.id for s in self.cited],
            "invalid_citation_ids": list(self.invalid_ids),
            "uncited": self.uncited,
        }


class CitationRewriter:
    """Rewrites ``[S<n>]`` markers to sequential ``[k]`` as text streams through."""

    def __init__(self, registry: SourceRegistry) -> None:
        self._registry = registry
        self._buffer = ""
        self._numbers: dict[str, int] = {}
        self.report = CitationReport()

    def feed(self, chunk: str) -> str:
        """Consume a delta and return the text that is safe to display now."""
        self._buffer += chunk
        rewritten = CITATION_RE.sub(self._replace, self._buffer)
        match = PARTIAL_RE.search(rewritten)
        if match:
            self._buffer = rewritten[match.start() :]
            return rewritten[: match.start()]
        self._buffer = ""
        return rewritten

    def flush(self) -> str:
        """Emit whatever is still held back, resolving any final citation."""
        remaining = CITATION_RE.sub(self._replace, self._buffer)
        self._buffer = ""
        return remaining

    def rewrite(self, text: str) -> str:
        """Non-streaming convenience: feed everything and flush."""
        return self.feed(text) + self.flush()

    def _replace(self, match: re.Match[str]) -> str:
        leading, ids = match.group(1), match.group(2)
        out: list[str] = []
        for raw_id in ids.split(","):
            source_id = raw_id.strip().upper()
            source = self._registry.get(source_id)
            if source is None:
                if source_id not in self.report.invalid_ids:
                    self.report.invalid_ids.append(source_id)
                continue  # drop invented references entirely (PLAN §13)
            number = self._numbers.get(source.id)
            if number is None:
                number = len(self._numbers) + 1
                self._numbers[source.id] = number
                self.report.cited.append(source)
            out.append(f"[{number}]")
        # Nothing valid survived: drop the marker and the space that preceded it.
        return leading + "".join(out) if out else ""


def build_sources_section(report: CitationReport, registry: SourceRegistry) -> str:
    """Render the Sources block from URLs Tavily actually returned."""
    if report.cited:
        lines = [
            f"[{index}] {source.title} — {source.url}"
            for index, source in enumerate(report.cited, start=1)
        ]
        return "Sources\n" + "\n".join(lines)
    retrieved = registry.all()
    if not retrieved:
        return ""
    # Nothing was cited: list what was retrieved, without implying per-claim attribution.
    lines = [f"[{s.number}] {s.title} — {s.url}" for s in retrieved]
    return "Sources consulted\n" + "\n".join(lines)


def tidy(text: str) -> str:
    """Clean up spacing left behind when invalid citations were removed."""
    text = re.sub(r"[ \t]+([.,;:!?])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()
