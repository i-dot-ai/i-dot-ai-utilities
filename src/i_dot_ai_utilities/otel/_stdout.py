"""Format and attach single-line JSON stdout logging alongside OTLP export."""

from __future__ import annotations

import datetime as dt
import json
import logging
import sys
from typing import Any, Final

from opentelemetry import trace
from opentelemetry.sdk._logs import LoggingHandler

# Standard LogRecord attributes that Python sets on every record. Any other attribute on vars(record)
# came from extra={...} and is copied into the JSON payload.
_RESERVED_RECORD_ATTRS: Final[frozenset[str]] = frozenset(
    {*vars(logging.LogRecord("", 0, "", 0, "", (), None)), "message", "asctime", "taskName"}
)


class JsonLineFormatter(logging.Formatter):
    """Serialize each log record as a single-line JSON object so `awslogs` keeps multi-line tracebacks in one event.

    Emits `timestamp`, `level`, `logger`, and `message` on every record, adds
    hex `trace_id` and `span_id` when an OpenTelemetry span is active, copies
    all `extra` attributes, and serializes `exc_info` into `exception` with
    escaped newlines.
    """

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
    """Stream JSON log lines to `sys.stdout` instead of `StreamHandler`'s default `sys.stderr`.

    Binding `sys.stdout` on a distinct `StreamHandler` subclass keeps fallback
    application logs on the same stream as EMF metrics and lets callers tell
    this handler apart from application console handlers.
    """

    def __init__(self) -> None:
        super().__init__(sys.stdout)
        self.setFormatter(JsonLineFormatter())


def install_stdout_handler() -> JsonStdoutHandler | None:
    """Attach `JsonStdoutHandler` to the root logger when no application handler is present.

    Any existing root handler other than OpenTelemetry's `LoggingHandler`
    indicates that the application already configured its own output, so this
    function leaves the root logger unchanged to avoid duplicate stdout lines.

    Returns:
        The attached `JsonStdoutHandler`, or `None` if the root logger already
        has a non-OpenTelemetry handler.
    """
    root = logging.getLogger()
    if any(not isinstance(handler, LoggingHandler) for handler in root.handlers):
        return None
    handler = JsonStdoutHandler()
    root.addHandler(handler)
    return handler


__all__ = ["JsonLineFormatter", "JsonStdoutHandler", "install_stdout_handler"]
