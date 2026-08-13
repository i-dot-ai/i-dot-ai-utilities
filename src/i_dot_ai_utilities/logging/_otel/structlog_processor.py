"""Structlog processor that injects OTel trace context into every log event.

Replaces the existing middleware's per-lifecycle-event ``set_context_field``
calls for ``trace_id`` / ``span_id`` / ``trace_flags``. Because the processor
runs on *every* event in the structlog pipeline, any log line emitted inside
a Django request — not just ``request_started`` / ``request_completed`` —
gets trace correlation automatically.

Field names and encodings follow the OTel logs data model:

- ``trace_id`` — 32 lowercase hex chars
- ``span_id`` — 16 lowercase hex chars
- ``trace_flags`` — 2 lowercase hex chars

Keys are omitted entirely when there is no active span (matching the
existing middleware's "don't emit empty values" contract). Outside a span
context, downstream consumers should query with ``has(trace_id)`` rather
than ``trace_id=""``.

Pure Python module: imports only ``opentelemetry.trace``. No side effects
on import.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from opentelemetry import trace

if TYPE_CHECKING:
    from collections.abc import MutableMapping


_TRACE_ID_HEX_LEN = 32
_SPAN_ID_HEX_LEN = 16
_TRACE_FLAGS_HEX_LEN = 2


def otel_trace_context_processor(
    _logger: Any,
    _method_name: str,
    event_dict: MutableMapping[str, Any],
) -> MutableMapping[str, Any]:
    """Inject ``trace_id`` / ``span_id`` / ``trace_flags`` from the active span.

    Signature matches the structlog processor contract
    (``logger, method_name, event_dict``). Mutates and returns ``event_dict``
    in place, as is idiomatic for structlog processors.

    Behaviour:

    - Active, recording, valid span context: all three keys are injected.
    - No active span, or non-recording span, or invalid span context: all
      three keys are omitted. Pre-existing ``trace_id`` / ``span_id`` /
      ``trace_flags`` entries in the event dict are left untouched on the
      absence path so the processor is non-destructive.
    - Unexpected exception while reading the span context: swallowed. An
      observability helper must never take a log line down; the worst case
      is we lose trace correlation for a single event, not that the event
      goes unlogged.
    """
    with contextlib.suppress(Exception):
        span = trace.get_current_span()
        ctx = span.get_span_context()
        if ctx.is_valid:
            event_dict["trace_id"] = format(ctx.trace_id, f"0{_TRACE_ID_HEX_LEN}x")
            event_dict["span_id"] = format(ctx.span_id, f"0{_SPAN_ID_HEX_LEN}x")
            event_dict["trace_flags"] = format(ctx.trace_flags, f"0{_TRACE_FLAGS_HEX_LEN}x")
    return event_dict


__all__ = ["otel_trace_context_processor"]
