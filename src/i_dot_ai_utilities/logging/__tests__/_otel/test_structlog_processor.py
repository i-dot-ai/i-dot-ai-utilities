# mypy: disable-error-code="no-untyped-def"
"""Tests for ``_otel.structlog_processor.otel_trace_context_processor``."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from i_dot_ai_utilities.logging._otel.structlog_processor import (
    otel_trace_context_processor,
)


def _ensure_provider() -> None:
    current = trace.get_tracer_provider()
    if type(current).__name__ == "ProxyTracerProvider":
        trace.set_tracer_provider(TracerProvider())


def test_injects_trace_span_and_flags_inside_span():
    _ensure_provider()
    tracer = trace.get_tracer(__name__)
    event_dict: dict[str, Any] = {"event": "hello"}
    with tracer.start_as_current_span("probe"):
        result = otel_trace_context_processor(None, "info", event_dict)

    assert "trace_id" in result
    assert "span_id" in result
    assert "trace_flags" in result
    # Formats: 32 hex, 16 hex, 2 hex, all lowercase.
    assert len(result["trace_id"]) == 32
    assert len(result["span_id"]) == 16
    assert len(result["trace_flags"]) == 2
    assert result["trace_id"] == result["trace_id"].lower()
    assert result["span_id"] == result["span_id"].lower()


def test_omits_keys_when_no_active_span():
    event_dict: dict[str, Any] = {"event": "hello"}
    result = otel_trace_context_processor(None, "info", event_dict)
    assert "trace_id" not in result
    assert "span_id" not in result
    assert "trace_flags" not in result


def test_returns_same_dict_object():
    """Structlog processors are expected to mutate and return in place."""
    event_dict: dict[str, Any] = {"event": "hello"}
    result = otel_trace_context_processor(None, "info", event_dict)
    assert result is event_dict


def test_preserves_existing_keys():
    _ensure_provider()
    tracer = trace.get_tracer(__name__)
    event_dict: dict[str, Any] = {"event": "hello", "other_key": "value"}
    with tracer.start_as_current_span("probe"):
        result = otel_trace_context_processor(None, "info", event_dict)
    assert result["event"] == "hello"
    assert result["other_key"] == "value"
    assert "trace_id" in result


def test_existing_trace_id_left_untouched_when_no_span():
    """If a caller pre-set ``trace_id`` and there is no active span, the
    processor must not blow it away."""
    event_dict: dict[str, Any] = {"event": "hello", "trace_id": "pre-existing"}
    result = otel_trace_context_processor(None, "info", event_dict)
    assert result["trace_id"] == "pre-existing"


def test_broken_span_context_swallowed():
    """A misbehaving span must never take the log line down."""

    class _BrokenSpan:
        def get_span_context(self) -> Any:
            msg = "boom"
            raise RuntimeError(msg)

    event_dict: dict[str, Any] = {"event": "hello"}
    with patch(
        "i_dot_ai_utilities.logging._otel.structlog_processor.trace.get_current_span",
        return_value=_BrokenSpan(),
    ):
        result = otel_trace_context_processor(None, "info", event_dict)

    # No keys injected, no exception raised, original event preserved.
    assert "trace_id" not in result
    assert result["event"] == "hello"


@pytest.mark.parametrize("method_name", ["debug", "info", "warning", "error", "exception"])
def test_works_for_every_method_name(method_name):
    _ensure_provider()
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("probe"):
        result = otel_trace_context_processor(None, method_name, {"event": "x"})
    assert "trace_id" in result
