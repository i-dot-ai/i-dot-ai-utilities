"""Best-effort OTLP log emit from structlog events."""

from __future__ import annotations

import contextlib
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import MutableMapping


def otel_otlp_log_emitter_processor(
    _logger: Any,
    method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Emit an OTLP log record; never raise into the structlog pipeline."""
    with contextlib.suppress(Exception):
        from opentelemetry import context as otel_context  # noqa: PLC0415
        from opentelemetry._logs import LogRecord, SeverityNumber, get_logger  # noqa: PLC0415

        severity_map = {
            "debug": SeverityNumber.DEBUG,
            "info": SeverityNumber.INFO,
            "warning": SeverityNumber.WARN,
            "warn": SeverityNumber.WARN,
            "error": SeverityNumber.ERROR,
            "exception": SeverityNumber.ERROR,
            "critical": SeverityNumber.FATAL,
            "fatal": SeverityNumber.FATAL,
        }
        level = str(event_dict.get("level", method_name)).lower()
        body = str(event_dict.get("event") or event_dict.get("message") or "")
        attrs: dict[str, Any] = {}
        for key, value in event_dict.items():
            if key in {"event", "message", "level", "timestamp", "_record", "_from_structlog"}:
                continue
            if isinstance(value, str | int | float | bool):
                attrs[key] = value

        # Logger.emit(record) is the only signature the OTel API defines. Passing
        # context lets the SDK correlate the log to the active span (trace_id/span_id).
        # timestamp must be set: the SDK leaves Timestamp unset → OTLP time_unix_nano=0
        # → OpenSearch @timestamp=1970 when the exporter maps Timestamp only.
        record = LogRecord(
            timestamp=time.time_ns(),
            context=otel_context.get_current(),
            body=body,
            severity_text=level.upper(),
            severity_number=severity_map.get(level, SeverityNumber.INFO),
            attributes=attrs,
        )
        get_logger("i_dot_ai_utilities.logging").emit(record)
    return event_dict


__all__ = ["otel_otlp_log_emitter_processor"]
