"""Single-line JSON stdout logging, kept alongside OTLP as a fallback."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggingHandler

# Attributes every ``LogRecord`` has; anything else on the record came from ``extra=``.
_RESERVED_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {*vars(logging.LogRecord("", 0, "", 0, "", (), None)), "message", "asctime", "taskName"}
)


class JsonLineFormatter(logging.Formatter):
    """Format each record as one JSON object on one line, so ``awslogs`` keeps it as one event."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": dt.datetime.fromtimestamp(record.created, tz=dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")
        for key, value in vars(record).items():
            if key not in _RESERVED_RECORD_ATTRS:
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, separators=(",", ":"))


class JsonStdoutHandler(logging.StreamHandler):  # type: ignore[type-arg]
    """Stdout handler that writes :class:`JsonLineFormatter` lines."""

    def __init__(self) -> None:
        super().__init__(sys.stdout)
        self.setFormatter(JsonLineFormatter())


def install_stdout_handler() -> JsonStdoutHandler | None:
    """Add a JSON stdout handler to the root logger unless the app already has its own.

    Any non-OpenTelemetry handler already on the root logger counts as the
    application's own console logging, so it is left alone and nothing is added.
    """
    root = logging.getLogger()
    if any(not isinstance(handler, LoggingHandler) for handler in root.handlers):
        return None
    handler = JsonStdoutHandler()
    root.addHandler(handler)
    return handler


__all__ = ["JsonLineFormatter", "JsonStdoutHandler", "install_stdout_handler"]
