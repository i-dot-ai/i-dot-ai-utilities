"""Tests for OTLP export resilience: outage circuit breaker, tracked batching, and CloudWatch EMF alarms."""

from __future__ import annotations

import json
import logging
import warnings
from typing import TYPE_CHECKING

from opentelemetry.context import Context
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.trace import TracerProvider

from i_dot_ai_utilities.otel import _emf
from i_dot_ai_utilities.otel.__tests__.conftest import ControllableLogExporter, FakeClock

if TYPE_CHECKING:
    import pytest
from i_dot_ai_utilities.otel._emf import (
    EMF_EVENT_NAME,
    EMF_METRIC_NAME,
    EMF_NAMESPACE,
    RateLimitedFailureSignal,
)
from i_dot_ai_utilities.otel._export import (
    ExportCircuitBreaker,
    FailureReportingExporter,
    TrackedBatchLogRecordProcessor,
    TrackedLogRecordExporter,
)
from i_dot_ai_utilities.otel._lambda import emit_lambda_export_failure, flush_lambda_logs


class TestExportCircuitBreaker:
    """Process-local breaker skips synchronous Lambda flushes for a cooldown window after an outage."""

    def test_opens_on_failure_and_closes_once_cooldown_window_elapses(self) -> None:
        clock = FakeClock()
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=clock)
        assert not breaker.is_open()

        breaker.record_failure()
        assert breaker.is_open()

        clock.advance(29.9)
        assert breaker.is_open()

        clock.advance(0.2)
        assert not breaker.is_open()

    def test_closes_immediately_on_successful_export(self) -> None:
        clock = FakeClock()
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=clock)
        breaker.record_failure()
        assert breaker.is_open()

        breaker.record_success()
        assert not breaker.is_open()


class TestTrackedExporterAndBatchProcessor:
    """Tracked Lambda export records failures on the breaker and caps each outage flush to a single attempt."""

    def test_records_failed_status_and_raised_exceptions_on_circuit_breaker(self) -> None:
        clock = FakeClock()
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=clock)
        delegate = ControllableLogExporter(result=LogRecordExportResult.FAILURE)
        tracked = TrackedLogRecordExporter(delegate, breaker)

        assert tracked.export([]) is LogRecordExportResult.FAILURE
        assert breaker.is_open()
        assert tracked.consume_failure() is True
        assert tracked.consume_failure() is False

        breaker.record_success()
        delegate.raise_on_export = True
        assert tracked.export([]) is LogRecordExportResult.FAILURE
        assert breaker.is_open()
        assert tracked.consume_failure() is True
        assert tracked.force_flush() is True
        tracked.shutdown()
        assert delegate.shutdown_called is True

    def test_buffers_records_in_fifo_order_preserving_trace_ids_while_breaker_is_open_and_flushes_after_cooldown(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Open-breaker records clear ``Context()`` to avoid pinning spans in memory while keeping hex IDs and order."""
        monkeypatch.setenv("OTEL_BLRP_SCHEDULE_DELAY", "60000")
        tracer = TracerProvider().get_tracer("test.breaker.fifo")
        clock = FakeClock()
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=clock)
        delegate = ControllableLogExporter(result=LogRecordExportResult.SUCCESS)
        processor = TrackedBatchLogRecordProcessor(TrackedLogRecordExporter(delegate, breaker))
        provider = LoggerProvider()
        provider.add_log_record_processor(processor)

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)

        test_logger = logging.getLogger("test.breaker.fifo")
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False
        test_logger.addHandler(handler)
        try:
            breaker.record_failure()
            with tracer.start_as_current_span("invocation") as span:
                expected_tid = span.get_span_context().trace_id
                expected_sid = span.get_span_context().span_id
                test_logger.info("msg-1")
                test_logger.info("msg-2")

            assert delegate.batches == []
            assert processor._queue is not None  # noqa: SLF001
            assert all(rec.log_record.context == Context() for rec in processor._queue)  # noqa: SLF001

            clock.advance(30.1)
            assert processor.force_flush() is True
            assert len(delegate.batches) == 1
            exported = delegate.batches[0]
            assert [rec.log_record.body for rec in exported] == ["msg-1", "msg-2"]
            assert [rec.log_record.trace_id for rec in exported] == [expected_tid, expected_tid]
            assert [rec.log_record.span_id for rec in exported] == [expected_sid, expected_sid]
        finally:
            test_logger.removeHandler(handler)
            processor.shutdown()

    def test_open_breaker_drops_oldest_records_when_bounded_queue_fills(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OTEL_BLRP_MAX_QUEUE_SIZE", "2")
        monkeypatch.setenv("OTEL_BLRP_MAX_EXPORT_BATCH_SIZE", "2")
        monkeypatch.setenv("OTEL_BLRP_SCHEDULE_DELAY", "60000")
        clock = FakeClock()
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=clock)
        delegate = ControllableLogExporter(result=LogRecordExportResult.SUCCESS)
        processor = TrackedBatchLogRecordProcessor(TrackedLogRecordExporter(delegate, breaker))
        provider = LoggerProvider()
        provider.add_log_record_processor(processor)

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)

        test_logger = logging.getLogger("test.breaker.eviction")
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False
        test_logger.addHandler(handler)
        try:
            breaker.record_failure()
            test_logger.info("msg-1")
            test_logger.info("msg-2")
            test_logger.info("msg-3")

            clock.advance(30.1)
            assert processor.force_flush() is True
            assert [rec.log_record.body for rec in delegate.batches[0]] == ["msg-2", "msg-3"]
        finally:
            test_logger.removeHandler(handler)
            processor.shutdown()

    def test_aborts_remaining_queued_batches_after_first_failed_export(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Once one batch fails, remaining queued batches fail immediately instead of multiplying timeouts."""
        monkeypatch.setenv("OTEL_BLRP_MAX_EXPORT_BATCH_SIZE", "2")
        monkeypatch.setenv("OTEL_BLRP_SCHEDULE_DELAY", "60000")
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=FakeClock())
        delegate = ControllableLogExporter(result=LogRecordExportResult.FAILURE)
        tracked_exporter = TrackedLogRecordExporter(delegate, breaker)
        processor = TrackedBatchLogRecordProcessor(tracked_exporter)
        provider = LoggerProvider()
        provider.add_log_record_processor(processor)

        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", category=DeprecationWarning)
            handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)

        test_logger = logging.getLogger("test.outage")
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False
        test_logger.addHandler(handler)
        failures: list[str] = []
        try:
            # Queue 3 batches of 2 records while the breaker is open, then attempt one invocation flush.
            breaker.record_failure()
            for idx in range(6):
                test_logger.info("record-%d", idx)
            breaker.record_success()

            flushed = flush_lambda_logs(
                None,
                processor=processor,
                breaker=breaker,
                failure_emitter=failures.append,
            )
            assert flushed is False
            assert len(delegate.batches) == 1
            assert breaker.is_open()
            assert failures == ["export_failure"]
        finally:
            test_logger.removeHandler(handler)
            processor.shutdown()


class TestFailureReportingExporter:
    """Container runtimes wrap the background exporter so failed batches trigger the EMF signal."""

    def test_invokes_failure_callback_when_delegate_raises_and_stays_silent_on_success(self) -> None:
        reasons: list[str] = []

        class _RaisingExporter(InMemoryLogRecordExporter):
            def export(self, _batch: object) -> LogRecordExportResult:  # type: ignore[override]
                raise RuntimeError

        assert FailureReportingExporter(_RaisingExporter(), reasons.append).export([]) is LogRecordExportResult.FAILURE
        assert FailureReportingExporter(InMemoryLogRecordExporter(), reasons.append).export([]) is (
            LogRecordExportResult.SUCCESS
        )
        assert reasons == ["export_failure"]

    def test_invokes_failure_callback_when_delegate_returns_failure_status_and_delegates_lifecycle(self) -> None:
        reasons: list[str] = []
        delegate = ControllableLogExporter(result=LogRecordExportResult.FAILURE, flush_result=False)
        exporter = FailureReportingExporter(delegate, reasons.append)

        assert exporter.export([]) is LogRecordExportResult.FAILURE
        assert reasons == ["export_failure"]
        assert exporter.force_flush() is False
        exporter.shutdown()
        assert delegate.shutdown_called is True


class TestCloudWatchEmfSignals:
    """Failed exports write sanitised CloudWatch EMF JSON lines to stdout so alarms fire during outages."""

    def test_lambda_failure_signal_writes_single_cloudwatch_emf_warning_line(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "core-auth-api-authorise-token")
        emit_lambda_export_failure("export_failure")

        captured = capsys.readouterr()
        lines = [line for line in captured.out.splitlines() if line.strip()]
        assert len(lines) == 1
        assert captured.err == ""

        payload = json.loads(lines[0])
        assert payload["level"] == "WARNING"
        assert payload["event"] == EMF_EVENT_NAME
        assert payload["reason"] == "export_failure"
        assert payload["FunctionName"] == "core-auth-api-authorise-token"
        assert payload[EMF_METRIC_NAME] == 1

        cw = payload["_aws"]["CloudWatchMetrics"][0]
        assert cw["Namespace"] == EMF_NAMESPACE
        assert cw["Dimensions"] == [["FunctionName"]]
        assert cw["Metrics"] == [{"Name": EMF_METRIC_NAME, "Unit": "Count"}]

    def test_container_failure_signal_rate_limits_repeated_failures_within_window(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Background container exports can fail every second; rate-limiting prevents stdout flooding."""
        signal_ = RateLimitedFailureSignal(dimension="ServiceName", value="svc", interval_seconds=60)
        signal_("export_failure")
        signal_("export_failure")

        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["ServiceName"] == "svc"

    def test_container_failure_signal_emits_again_once_interval_elapses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        clock = FakeClock(100.0)
        monkeypatch.setattr(_emf.time, "monotonic", clock)
        signal_ = RateLimitedFailureSignal(dimension="ServiceName", value="svc", interval_seconds=60.0)

        signal_("export_failure")
        clock.advance(59.9)
        signal_("export_failure")
        clock.advance(0.2)
        signal_("export_failure")

        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 2
        assert [json.loads(line)["reason"] for line in lines] == ["export_failure", "export_failure"]
