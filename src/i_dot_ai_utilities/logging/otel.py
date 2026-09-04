"""Public OpenTelemetry entry points for the logging package."""

from __future__ import annotations

from i_dot_ai_utilities.logging._otel import (
    build_composite_propagator,
    configure_otel,
    configure_otel_for_django,
    configure_otel_for_fastapi,
    configure_otel_for_lambda,
    ensure_structlog_otel_processors,
    force_flush_otel,
    insert_trace_processor,
    otel_trace_context_processor,
)

__all__ = [
    "build_composite_propagator",
    "configure_otel",
    "configure_otel_for_django",
    "configure_otel_for_fastapi",
    "configure_otel_for_lambda",
    "ensure_structlog_otel_processors",
    "force_flush_otel",
    "insert_trace_processor",
    "otel_trace_context_processor",
]
