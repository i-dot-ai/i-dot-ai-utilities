"""Tests for the OTLP log emitter structlog processor."""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry._logs import set_logger_provider
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    InMemoryLogExporter,
    SimpleLogRecordProcessor,
)
from opentelemetry.sdk.trace import TracerProvider

from i_dot_ai_utilities.logging._otel.otlp_log_processor import (
    otel_otlp_log_emitter_processor,
)

# OTel refuses to replace the global LoggerProvider once set, so install one
# shared provider for the module and clear the exporter between tests.
# SimpleLogRecordProcessor exports synchronously on emit, so tests can read the
# record back immediately.
_SHARED_EXPORTER = InMemoryLogExporter()
_SHARED_PROVIDER = LoggerProvider()
_SHARED_PROVIDER.add_log_record_processor(SimpleLogRecordProcessor(_SHARED_EXPORTER))


@pytest.fixture(autouse=True, scope="module")
def _install_logger_provider() -> None:
    set_logger_provider(_SHARED_PROVIDER)


@pytest.fixture
def log_exporter() -> InMemoryLogExporter:
    _SHARED_EXPORTER.clear()
    return _SHARED_EXPORTER


def test_returns_event_dict_unchanged() -> None:
    event = {"event": "hello", "level": "info", "user_id": 1}
    out = otel_otlp_log_emitter_processor(None, "info", event)
    assert out is event
    assert out["event"] == "hello"
    assert out["user_id"] == 1


def test_emits_record_with_body_severity_and_attributes(log_exporter: InMemoryLogExporter) -> None:
    event = {
        "event": "hello",
        "level": "info",
        "timestamp": "2026-07-29T12:00:00Z",
        "poc_run_id": "poc-ts-001",
    }

    otel_otlp_log_emitter_processor(None, "info", event)

    logs = log_exporter.get_finished_logs()
    assert len(logs) == 1
    record = logs[0].log_record
    assert record.body == "hello"
    assert record.severity_text == "INFO"
    assert record.attributes is not None
    # The structlog ``timestamp`` field is metadata, not a log attribute.
    assert dict(record.attributes) == {"poc_run_id": "poc-ts-001"}


def test_sets_nonzero_timestamp(log_exporter: InMemoryLogExporter) -> None:
    # A zero/unset timestamp maps to OTLP time_unix_nano=0, which surfaces as
    # @timestamp=1970 in OpenSearch, so the processor must always set it.
    otel_otlp_log_emitter_processor(None, "info", {"event": "hi", "level": "info"})

    record = log_exporter.get_finished_logs()[0].log_record
    assert isinstance(record.timestamp, int)
    assert record.timestamp > 0


def test_correlates_with_active_span(log_exporter: InMemoryLogExporter) -> None:
    tracer = TracerProvider().get_tracer(__name__)
    with tracer.start_as_current_span("work"):
        otel_otlp_log_emitter_processor(None, "info", {"event": "hi", "level": "info"})

    record = log_exporter.get_finished_logs()[0].log_record
    # Emitting with the active context lets the SDK stamp trace correlation.
    assert record.trace_id != 0
    assert record.span_id != 0
