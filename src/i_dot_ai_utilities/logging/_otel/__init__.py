"""OpenTelemetry integration subpackage for the logging module.

Public entry points:

- :func:`configure_otel_for_django` — one-call setup for Django services.
- :func:`otel_trace_context_processor` — structlog processor injecting
  ``trace_id`` / ``span_id`` / ``trace_flags`` from the active span.
- :func:`build_composite_propagator` — W3C + AWS X-Ray composite textmap.

Requires the ``[otel]`` optional extra. Importing this subpackage without
``opentelemetry-*`` installed raises ``ImportError`` at module load — kept
deliberately noisy so misconfiguration is caught during startup rather
than silently at first-request time.
"""

from __future__ import annotations

from i_dot_ai_utilities.logging._otel.propagators import build_composite_propagator
from i_dot_ai_utilities.logging._otel.setup import (
    configure_otel_for_django,
    insert_trace_processor,
)
from i_dot_ai_utilities.logging._otel.structlog_processor import (
    otel_trace_context_processor,
)

__all__ = [
    "build_composite_propagator",
    "configure_otel_for_django",
    "insert_trace_processor",
    "otel_trace_context_processor",
]
