"""One-line JSON structured logs (docs/PLAN.md §21)."""

from __future__ import annotations

import json
import logging
import sys
from typing import Any


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": round(record.created, 3),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if isinstance(extra, dict):
            payload.update(extra)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def configure(level: int = logging.INFO, stream: Any = sys.stderr) -> None:
    """Install the JSON formatter on the ``vnr`` logger tree. Idempotent."""
    logger = logging.getLogger("vnr")
    logger.setLevel(level)
    logger.propagate = False
    for handler in logger.handlers:
        if getattr(handler, "_vnr", False):
            return
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    handler._vnr = True  # type: ignore[attr-defined]
    logger.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"vnr.{name}")


def log(logger: logging.Logger, level: int, msg: str, /, **fields: Any) -> None:
    """Emit *msg* with structured *fields*."""
    logger.log(level, msg, extra={"fields": fields})
