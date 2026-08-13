# mypy: disable-error-code="no-untyped-def"
"""Tests for ``_otel.propagators.build_composite_propagator``."""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry import trace
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace.propagation.tracecontext import (
    TraceContextTextMapPropagator,
)

from i_dot_ai_utilities.logging._otel.propagators import (
    build_composite_propagator,
)

# W3C-formatted traceparent (sampled).
TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-01"
W3C_TRACE_ID_INT = 0x0AF7651916CD43DD8448EB211C80319C
W3C_SPAN_ID_INT = 0xB7AD6B7169203331

# X-Ray header format: `Root=1-<32hex>;Parent=<16hex>;Sampled=1`.
XRAY_HEADER = "Root=1-5759e988-bd862e3fe1be46a994272793;Parent=53995c3f42cd8ad8;Sampled=1"
XRAY_TRACE_ID_INT = 0x5759E988BD862E3FE1BE46A994272793
XRAY_SPAN_ID_INT = 0x53995C3F42CD8AD8


def _install_provider() -> None:
    # Attach a non-None tracer provider once per process so
    # ``SpanContext.extract`` survives into ``trace.get_current_span``.
    # The SDK refuses to replace an existing provider; rely on that
    # behaviour rather than forcing a swap.
    current = trace.get_tracer_provider()
    # ``ProxyTracerProvider`` is the uninitialised default. If we've
    # already set a real one, leave it in place.
    if type(current).__name__ == "ProxyTracerProvider":
        trace.set_tracer_provider(TracerProvider())


def test_returns_composite_with_two_members_in_order():
    prop = build_composite_propagator()
    assert isinstance(prop, CompositePropagator)

    # The CompositePropagator exposes members via its private attribute
    # in the SDK. Assert on behaviour rather than attribute name — run
    # ``inject`` and check both headers appear.
    carrier: dict[str, str] = {}
    _install_provider()
    set_global_textmap(prop)
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("probe"):
        from opentelemetry.propagate import inject  # noqa: PLC0415

        inject(carrier)
    # W3C always injects ``traceparent``; X-Ray injects
    # ``X-Amzn-Trace-Id``.
    assert "traceparent" in carrier
    assert "X-Amzn-Trace-Id" in carrier


def test_fields_property_covers_both_propagator_vocabularies():
    prop = build_composite_propagator()
    fields = set(prop.fields)
    # Each sub-propagator contributes its own field names.
    assert fields >= set(TraceContextTextMapPropagator().fields)
    assert fields >= set(AwsXRayPropagator().fields)


def test_extract_prefers_w3c_when_both_headers_present():
    prop = build_composite_propagator()
    _install_provider()
    set_global_textmap(prop)

    carrier = {
        "traceparent": TRACEPARENT,
        "X-Amzn-Trace-Id": XRAY_HEADER,
    }
    from opentelemetry.propagate import extract  # noqa: PLC0415

    ctx = extract(carrier)
    span = trace.get_current_span(ctx)
    sc = span.get_span_context()
    assert sc.is_valid
    assert sc.trace_id == W3C_TRACE_ID_INT
    assert sc.span_id == W3C_SPAN_ID_INT


def test_extract_falls_back_to_xray_when_no_w3c_header():
    prop = build_composite_propagator()
    _install_provider()
    set_global_textmap(prop)

    from opentelemetry.propagate import extract  # noqa: PLC0415

    ctx = extract({"X-Amzn-Trace-Id": XRAY_HEADER})
    span = trace.get_current_span(ctx)
    sc = span.get_span_context()
    assert sc.is_valid
    assert sc.trace_id == XRAY_TRACE_ID_INT
    assert sc.span_id == XRAY_SPAN_ID_INT


def test_extract_produces_invalid_context_when_no_recognised_headers():
    prop = build_composite_propagator()
    _install_provider()
    set_global_textmap(prop)

    from opentelemetry.propagate import extract  # noqa: PLC0415

    ctx = extract({"X-Random-Header": "irrelevant"})
    span = trace.get_current_span(ctx)
    sc = span.get_span_context()
    assert not sc.is_valid


def test_malformed_traceparent_falls_back_to_xray():
    """A broken ``traceparent`` must not stop X-Ray from being parsed."""
    prop = build_composite_propagator()
    _install_provider()
    set_global_textmap(prop)

    from opentelemetry.propagate import extract  # noqa: PLC0415

    ctx = extract(
        {
            # Version `ff` is explicitly invalid per W3C.
            "traceparent": "ff-" + "0" * 32 + "-" + "0" * 16 + "-00",
            "X-Amzn-Trace-Id": XRAY_HEADER,
        }
    )
    span = trace.get_current_span(ctx)
    sc = span.get_span_context()
    assert sc.is_valid
    assert sc.trace_id == XRAY_TRACE_ID_INT


def test_propagator_does_not_touch_x_request_id():
    """``X-Request-ID`` is log-correlation only — not a trace context."""
    prop = build_composite_propagator()
    _install_provider()
    set_global_textmap(prop)

    from opentelemetry.propagate import extract  # noqa: PLC0415

    ctx = extract({"X-Request-ID": "abc123"})
    span = trace.get_current_span(ctx)
    sc = span.get_span_context()
    assert not sc.is_valid
