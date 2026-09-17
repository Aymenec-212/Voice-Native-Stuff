"""Incremental parsing of a streaming STT process's stdout.

Command-line transcribers emit text in three shapes and often mix them: plain appended
words, a line redrawn with ``\\r``, and ANSI colour codes. This turns any of that into a
current transcript, one chunk at a time, with no assumption that chunks align to lines.
"""

from __future__ import annotations

import json
import re

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class TextStreamAccumulator:
    """Maintains the current transcript as raw output arrives."""

    def __init__(self) -> None:
        self._committed: list[str] = []
        self._current = ""

    @property
    def text(self) -> str:
        parts = [*self._committed, self._current]
        return " ".join(" ".join(parts).split())

    def feed(self, chunk: str) -> str:
        """Consume a chunk of stdout and return the transcript as it now stands."""
        chunk = ANSI_RE.sub("", chunk).replace("\x08", "")
        for char in chunk:
            if char == "\r":
                self._current = ""  # the tool is redrawing this line
            elif char == "\n":
                if self._current.strip():
                    self._committed.append(self._current.strip())
                self._current = ""
            else:
                self._current += char
        return self.text

    def reset(self) -> None:
        self._committed.clear()
        self._current = ""


class JsonLineAccumulator:
    """For runtimes that emit one JSON object per line: ``{"text": …, "final": bool}``.

    Unparseable lines are ignored rather than killing the session — a runtime that prints
    a banner or a warning to stdout should not end the utterance.
    """

    TEXT_KEYS = ("text", "transcript", "partial")

    def __init__(self) -> None:
        self._buffer = ""
        self.text = ""
        self.final_seen = False

    def feed(self, chunk: str) -> str:
        self._buffer += chunk
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            self._consume(line)
        return self.text

    def _consume(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict):
            return
        for key in self.TEXT_KEYS:
            value = payload.get(key)
            if isinstance(value, str):
                # A delta appends; a full transcript replaces.
                self.text = (self.text + value) if payload.get("delta") else value
                break
        if payload.get("final") or payload.get("is_final"):
            self.final_seen = True

    def reset(self) -> None:
        self._buffer = ""
        self.text = ""
        self.final_seen = False
