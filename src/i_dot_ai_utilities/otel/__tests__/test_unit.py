"""Unit tests for native OTel circuit breaker, tracked exporter/processor, EMF, and helpers."""

from __future__ import annotations

import io
import json
import logging
import warnings
from typing import TYPE_CHECKING

import pytest
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import (
    InMemoryLogRecordExporter,
    LogRecordExporter,
    LogRecordExportResult,
    SimpleLogRecordProcessor,
)

import i_dot_ai_utilities.otel as public_otel
from i_dot_ai_utilities.otel._configuration import (
    _ExcludeInternalOtelFilter,
    _install_root_handler_and_configure_provider,
    _reset_for_tests,
    configure_otel,
    detect_runtime,
)
from i_dot_ai_utilities.otel._emf import (
    EMF_EVENT_NAME,
    EMF_METRIC_NAME,
    EMF_NAMESPACE,
    emit_lambda_export_failure,
)
from i_dot_ai_utilities.otel._export import (
    DEFAULT_CIRCUIT_BREAKER_SECONDS,
    ENV_CIRCUIT_BREAKER_SECONDS,
    ExportCircuitBreaker,
    TrackedBatchLogRecordProcessor,
    TrackedLogRecordExporter,
    resolve_circuit_breaker_seconds,
)
from i_dot_ai_utilities.otel._lambda import (
    DEFAULT_FLUSH_MARGIN_MS,
    ENV_FLUSH_MARGIN_MS,
    flush_lambda_logs,
    is_lambda_runtime_stdout_handler,
    remove_lambda_runtime_stdout_handlers,
    resolve_flush_margin_ms,
    resolve_lambda_handler_target,
    resolve_logs_export_timeout_ms,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from opentelemetry.sdk._logs import ReadableLogRecord


@pytest.fixture(autouse=True)
def _clean_state() -> Iterator[None]:
    _reset_for_tests()
    yield
    _reset_for_tests()


class _FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _ControllableExporter(LogRecordExporter):
    def __init__(
        self,
        *,
        result: LogRecordExportResult = LogRecordExportResult.SUCCESS,
        raise_on_export: bool = False,
        flush_result: bool = True,
        raise_on_flush: bool = False,
    ) -> None:
        self.result = result
        self.raise_on_export = raise_on_export
        self.flush_result = flush_result
        self.raise_on_flush = raise_on_flush
        self.batches: list[list[ReadableLogRecord]] = []
        self.shutdown_called = False

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        self.batches.append(list(batch))
        if self.raise_on_export:
            msg = "network down"
            raise RuntimeError(msg)
        return self.result

    def force_flush(self, _timeout_millis: int = 30000) -> bool:
        if self.raise_on_flush:
            msg = "flush failed"
            raise RuntimeError(msg)
        return self.flush_result

    def shutdown(self) -> None:
        self.shutdown_called = True


class TestCircuitBreakerAndExporter:
    def test_circuit_breaker_opens_on_failure_and_half_opens_after_window(self) -> None:
        clock = _FakeClock()
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=clock)
        assert not breaker.is_open()

        breaker.record_failure()
        assert breaker.is_open()

        clock.advance(29.9)
        assert breaker.is_open()

        clock.advance(0.2)
        assert not breaker.is_open()

    def test_circuit_breaker_closes_on_success(self) -> None:
        clock = _FakeClock()
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=clock)
        breaker.record_failure()
        assert breaker.is_open()
        breaker.record_success()
        assert not breaker.is_open()

    def test_tracked_exporter_records_failure_and_exception(self) -> None:
        clock = _FakeClock()
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=clock)
        delegate = _ControllableExporter(result=LogRecordExportResult.FAILURE)
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

    def test_outage_flush_costs_one_export_not_one_per_batch(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OTEL_BLRP_MAX_EXPORT_BATCH_SIZE", "2")
        monkeypatch.setenv("OTEL_BLRP_SCHEDULE_DELAY", "60000")
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=_FakeClock())
        delegate = _ControllableExporter(result=LogRecordExportResult.FAILURE)
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
            # Keep records queued without waking the worker, as during an earlier outage.
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


class TestEmfSignal:
    def test_emit_lambda_export_failure_writes_single_valid_emf_line(
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


class TestLambdaHelpers:
    def test_resolve_env_values_and_invalid_fallbacks(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(ENV_CIRCUIT_BREAKER_SECONDS, raising=False)
        assert resolve_circuit_breaker_seconds() == DEFAULT_CIRCUIT_BREAKER_SECONDS

        monkeypatch.setenv(ENV_CIRCUIT_BREAKER_SECONDS, "not-a-float")
        assert resolve_circuit_breaker_seconds() == DEFAULT_CIRCUIT_BREAKER_SECONDS

        monkeypatch.setenv(ENV_CIRCUIT_BREAKER_SECONDS, "-5")
        assert resolve_circuit_breaker_seconds() == DEFAULT_CIRCUIT_BREAKER_SECONDS

        monkeypatch.setenv(ENV_CIRCUIT_BREAKER_SECONDS, "12.5")
        assert resolve_circuit_breaker_seconds() == 12.5

        monkeypatch.delenv(ENV_FLUSH_MARGIN_MS, raising=False)
        assert resolve_flush_margin_ms() == DEFAULT_FLUSH_MARGIN_MS

        monkeypatch.setenv(ENV_FLUSH_MARGIN_MS, "bad")
        assert resolve_flush_margin_ms() == DEFAULT_FLUSH_MARGIN_MS

        monkeypatch.setenv(ENV_FLUSH_MARGIN_MS, "-1")
        assert resolve_flush_margin_ms() == DEFAULT_FLUSH_MARGIN_MS

        monkeypatch.setenv(ENV_FLUSH_MARGIN_MS, "400")
        assert resolve_flush_margin_ms() == 400

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "bad")
        assert resolve_logs_export_timeout_ms() == 2000

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "0")
        assert resolve_logs_export_timeout_ms() == 2000

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "1.5")
        assert resolve_logs_export_timeout_ms() == 1500

    def test_resolve_lambda_handler_target_variants(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ORIG_HANDLER", raising=False)
        monkeypatch.delenv("_HANDLER", raising=False)
        assert resolve_lambda_handler_target() is None

        monkeypatch.setenv("_HANDLER", "invalid_no_dot")
        assert resolve_lambda_handler_target() is None

        monkeypatch.setenv("_HANDLER", "api/handlers/authorise_token.lambda_handler")
        assert resolve_lambda_handler_target() == (
            "api.handlers.authorise_token",
            "lambda_handler",
        )

        monkeypatch.setenv("ORIG_HANDLER", "pkg.mod.orig_handler")
        assert resolve_lambda_handler_target() == ("pkg.mod", "orig_handler")

    def test_remove_only_lambda_runtime_stdout_handlers(self) -> None:
        class LambdaLoggerHandler(logging.StreamHandler):  # type: ignore[type-arg]
            pass

        LambdaLoggerHandler.__module__ = "awslambdaric.bootstrap"
        runtime_handler = LambdaLoggerHandler(io.StringIO())
        custom_handler = logging.StreamHandler(io.StringIO())

        assert is_lambda_runtime_stdout_handler(runtime_handler) is True
        assert is_lambda_runtime_stdout_handler(custom_handler) is False

        test_logger = logging.getLogger("test.runtime.handlers")
        test_logger.handlers = [runtime_handler, custom_handler]
        removed = remove_lambda_runtime_stdout_handlers(test_logger)
        assert removed == 1
        assert test_logger.handlers == [custom_handler]

    def test_flush_lambda_logs_skips_when_remaining_time_below_budget(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "2")
        monkeypatch.setenv(ENV_FLUSH_MARGIN_MS, "250")

        clock = _FakeClock()
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=clock)
        delegate = _ControllableExporter()
        tracked_exporter = TrackedLogRecordExporter(delegate, breaker)
        processor = TrackedBatchLogRecordProcessor(tracked_exporter)

        class _LowTimeCtx:
            @staticmethod
            def get_remaining_time_in_millis() -> int:
                return 2200  # below 2000 + 250

        failures: list[str] = []
        try:
            flushed = flush_lambda_logs(
                _LowTimeCtx(),
                processor=processor,
                breaker=breaker,
                failure_emitter=failures.append,
            )
            assert flushed is False
            assert not breaker.is_open()
            assert failures == []
        finally:
            processor.shutdown()

    def test_detect_runtime_and_filter(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME", raising=False)
        assert detect_runtime() == "local"
        assert detect_runtime("lambda") == "lambda"

        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
        assert detect_runtime() == "lambda"
        assert detect_runtime("local") == "local"

        with pytest.raises(ValueError, match="Unsupported runtime"):
            detect_runtime("invalid")  # type: ignore[arg-type]

        otel_filter = _ExcludeInternalOtelFilter()
        internal_rec = logging.LogRecord("opentelemetry.sdk", logging.WARNING, "f", 1, "m", (), None)
        root_otel_rec = logging.LogRecord("opentelemetry", logging.WARNING, "f", 1, "m", (), None)
        app_rec = logging.LogRecord("api.handlers.authorise_token", logging.INFO, "f", 1, "m", (), None)
        assert otel_filter.filter(internal_rec) is False
        assert otel_filter.filter(root_otel_rec) is False
        assert otel_filter.filter(app_rec) is True

    def test_public_surface_exposes_only_configure_otel(self) -> None:
        assert public_otel.__all__ == ["configure_otel"]
        assert public_otel.configure_otel is configure_otel

    def test_library_loggers_default_to_warning_unless_app_set_a_level(self) -> None:
        names = ("botocore", "boto3", "urllib3")
        saved = {name: logging.getLogger(name).level for name in (*names, "")}
        root_logger = logging.getLogger()
        exporter = InMemoryLogRecordExporter()
        provider = LoggerProvider()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
        for name in names:
            logging.getLogger(name).setLevel(logging.NOTSET)
        logging.getLogger("urllib3").setLevel(logging.INFO)
        handler = _install_root_handler_and_configure_provider(provider, "local")
        try:
            logging.getLogger("botocore.credentials").info("Found credentials")
            logging.getLogger("botocore.credentials").warning("botocore-warning")
            logging.getLogger("urllib3").info("urllib3-info")

            bodies = [r.log_record.body for r in exporter.get_finished_logs()]
            assert bodies == ["botocore-warning", "urllib3-info"]
            assert logging.getLogger("boto3").level == logging.WARNING
            assert logging.getLogger("urllib3").level == logging.INFO
        finally:
            root_logger.removeHandler(handler)
            for name, level in saved.items():
                logging.getLogger(name).setLevel(level)
            provider.shutdown()
