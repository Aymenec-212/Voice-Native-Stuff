"""Separate explicitly tagged provider reasoning from user-facing answer text.

A tag may cross any SSE boundary. Hold only a possible tag prefix so ordinary answer
text still streams immediately. Untagged prose is not guessed to be reasoning.
"""


class ReasoningSplitter:
    def __init__(self) -> None:
        self.pending = ""
        self.thinking = False

    def feed(self, text: str, *, final: bool = False) -> tuple[str, str]:
        self.pending += text
        answer, reasoning = [], []
        tags = ("<think>", "</think>")
        while self.pending:
            found = [(self.pending.find(tag), tag) for tag in tags if tag in self.pending]
            if found:
                index, tag = min(found)
                (reasoning if self.thinking else answer).append(self.pending[:index])
                self.pending = self.pending[index + len(tag) :]
                self.thinking = tag == "<think>"
                continue
            keep = 0
            if not final:
                for size in range(1, min(len(self.pending), max(map(len, tags)) - 1) + 1):
                    if any(tag.startswith(self.pending[-size:]) for tag in tags):
                        keep = size
            take = len(self.pending) - keep
            (reasoning if self.thinking else answer).append(self.pending[:take])
            self.pending = self.pending[take:]
            break
        return "".join(answer), "".join(reasoning)
