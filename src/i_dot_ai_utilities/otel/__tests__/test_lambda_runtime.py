"""Unit and subprocess integration tests for the AWS Lambda OpenTelemetry runtime."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import types
from typing import TYPE_CHECKING, Any

import pytest
from opentelemetry._logs import LogRecord
from opentelemetry.sdk._logs import ReadWriteLogRecord

if TYPE_CHECKING:
    from collections.abc import Sequence

    from opentelemetry.sdk._logs import ReadableLogRecord
    from opentelemetry.sdk._logs.export import LogRecordExportResult

from i_dot_ai_utilities.otel import _lambda
from i_dot_ai_utilities.otel.__tests__.conftest import ControllableLogExporter, FakeClock
from i_dot_ai_utilities.otel.__tests__.fixtures.otlp_capture_server import OtlpCaptureServer
from i_dot_ai_utilities.otel._emf import EMF_EVENT_NAME, EMF_METRIC_NAME, EMF_NAMESPACE
from i_dot_ai_utilities.otel._environment import ENV_KEEP_STDOUT_LOGS
from i_dot_ai_utilities.otel._export import (
    DEFAULT_CIRCUIT_BREAKER_SECONDS,
    ENV_CIRCUIT_BREAKER_SECONDS,
    ExportCircuitBreaker,
    LambdaLogRecordProcessor,
    resolve_circuit_breaker_seconds,
)
from i_dot_ai_utilities.otel._instrumentation import uninstrument_installed_libraries
from i_dot_ai_utilities.otel._lambda import (
    DEFAULT_FLUSH_MARGIN_MS,
    ENV_FLUSH_MARGIN_MS,
    LambdaRuntime,
    flush_lambda_logs,
    instrument_lambda_handler,
    is_lambda_runtime_stdout_handler,
    remove_lambda_runtime_stdout_handlers,
    resolve_flush_margin_ms,
    resolve_lambda_handler_target,
    resolve_logs_export_timeout_ms,
)

_HANDLER_PATH = "i_dot_ai_utilities.otel.__tests__.fixtures.sample_lambda_handler.lambda_handler"
_RUNNER_MODULE = "i_dot_ai_utilities.otel.__tests__.fixtures.run_lambda_scenario"


def _base_lambda_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("OTEL_") and not k.startswith("I_DOT_AI_OTEL_") and not k.startswith("TEST_")
    }
    env.update(
        {
            "AWS_LAMBDA_FUNCTION_NAME": "core-auth-api-authorise-token",
            "AWS_LAMBDA_FUNCTION_VERSION": "$LATEST",
            "AWS_LAMBDA_LOG_STREAM_NAME": "2026/10/16/[$LATEST]abcdef",
            "AWS_LAMBDA_FUNCTION_MEMORY_SIZE": "512",
            "AWS_REGION": "eu-west-2",
            "_HANDLER": _HANDLER_PATH,
        }
    )
    if extra:
        env.update(extra)
    return env


def _run_scenario(
    invocations: list[dict[str, Any]],
    *,
    env_overrides: dict[str, str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    payload = json.dumps({"invocations": invocations})
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", _RUNNER_MODULE, payload],
        capture_output=True,
        text=True,
        env=_base_lambda_env(env_overrides),
        check=False,
    )
    assert proc.returncode == 0, f"subprocess failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"

    scenario_json: dict[str, Any] | None = None
    for line in proc.stderr.splitlines():
        if line.startswith("SCENARIO_JSON:"):
            scenario_json = json.loads(line.removeprefix("SCENARIO_JSON:"))
            break
    assert scenario_json is not None, f"missing SCENARIO_JSON in stderr={proc.stderr!r}"
    return proc, scenario_json


class TestLambdaEnvAndHandlerResolution:
    """Invalid environment settings fall back to safe defaults rather than crashing cold start."""

    def test_circuit_breaker_seconds_reads_env_and_falls_back_on_invalid_or_negative_values(
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

    def test_flush_margin_ms_reads_env_and_falls_back_on_invalid_or_negative_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(ENV_FLUSH_MARGIN_MS, raising=False)
        assert resolve_flush_margin_ms() == DEFAULT_FLUSH_MARGIN_MS

        monkeypatch.setenv(ENV_FLUSH_MARGIN_MS, "bad")
        assert resolve_flush_margin_ms() == DEFAULT_FLUSH_MARGIN_MS

        monkeypatch.setenv(ENV_FLUSH_MARGIN_MS, "-1")
        assert resolve_flush_margin_ms() == DEFAULT_FLUSH_MARGIN_MS

        monkeypatch.setenv(ENV_FLUSH_MARGIN_MS, "400")
        assert resolve_flush_margin_ms() == 400

    def test_logs_export_timeout_converts_seconds_to_ms_and_rejects_non_positive_values(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Python's OTLP HTTP exporter reads ``OTEL_EXPORTER_OTLP_LOGS_TIMEOUT`` in seconds, not milliseconds."""
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "bad")
        assert resolve_logs_export_timeout_ms() == 2000

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "0")
        assert resolve_logs_export_timeout_ms() == 2000

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "1.5")
        assert resolve_logs_export_timeout_ms() == 1500

        monkeypatch.delenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "3")
        assert resolve_logs_export_timeout_ms() == 3000

        LambdaRuntime(service_name="svc").apply_env_defaults(can_export=True)
        assert "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT" not in os.environ

        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TIMEOUT")
        LambdaRuntime(service_name="svc").apply_env_defaults(can_export=True)
        assert os.environ["OTEL_EXPORTER_OTLP_LOGS_TIMEOUT"] == "2"

    def test_resolves_handler_target_from_orig_handler_or_slash_delimited_handler(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("ORIG_HANDLER", raising=False)
        monkeypatch.delenv("_HANDLER", raising=False)
        assert resolve_lambda_handler_target() is None

        for invalid in ("invalid_no_dot", ".missing_module", "missing_func."):
            monkeypatch.setenv("_HANDLER", invalid)
            assert resolve_lambda_handler_target() is None

        monkeypatch.setenv("_HANDLER", "api/handlers/authorise_token.lambda_handler")
        assert resolve_lambda_handler_target() == ("api.handlers.authorise_token", "lambda_handler")

        monkeypatch.setenv("ORIG_HANDLER", "pkg.mod.orig_handler")
        assert resolve_lambda_handler_target() == ("pkg.mod", "orig_handler")

    def test_warns_and_skips_wrapping_when_handler_env_is_unset_or_module_import_fails(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        monkeypatch.delenv("ORIG_HANDLER", raising=False)
        monkeypatch.delenv("_HANDLER", raising=False)
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=FakeClock())

        assert instrument_lambda_handler(processor=None, breaker=breaker) is False
        assert "ORIG_HANDLER/_HANDLER is not set; skipping Lambda handler wrapping." in capsys.readouterr().err

        monkeypatch.setenv("_HANDLER", "nonexistent_lambda_module_xyz.handler")
        assert instrument_lambda_handler(processor=None, breaker=breaker) is False
        assert (
            "failed to import handler module 'nonexistent_lambda_module_xyz'; skipping handler wrapping."
            in capsys.readouterr().err
        )


class TestLambdaStdoutHandlers:
    """``awslambdaric`` root handlers are removed only when OTLP export and per-invocation flushing are both active."""

    def test_removes_only_awslambdaric_handlers_when_stdout_opted_out(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")

        class LambdaLoggerHandler(logging.StreamHandler):  # type: ignore[type-arg]
            pass

        LambdaLoggerHandler.__module__ = "awslambdaric.bootstrap"
        runtime_handler = LambdaLoggerHandler(io.StringIO())
        custom_handler = logging.StreamHandler(io.StringIO())

        test_logger = logging.getLogger("test.runtime.handlers")
        test_logger.handlers = [runtime_handler, custom_handler]
        monkeypatch.delenv(ENV_KEEP_STDOUT_LOGS, raising=False)
        assert remove_lambda_runtime_stdout_handlers(test_logger) == 0

        monkeypatch.setenv(ENV_KEEP_STDOUT_LOGS, "false")
        assert is_lambda_runtime_stdout_handler(runtime_handler) is True
        assert is_lambda_runtime_stdout_handler(custom_handler) is False

        removed = remove_lambda_runtime_stdout_handlers(test_logger)
        assert removed == 1
        assert test_logger.handlers == [custom_handler]

    @pytest.mark.parametrize(
        "exporter",
        [
            pytest.param("none", id="lowercase-none"),
            pytest.param("None", id="capitalised-none"),
            pytest.param(" NONE ", id="padded-none"),
            pytest.param("", id="empty-string"),
        ],
    )
    def test_keeps_awslambdaric_handlers_when_otlp_export_disabled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        exporter: str,
    ) -> None:
        class LambdaLoggerHandler(logging.StreamHandler):  # type: ignore[type-arg]
            pass

        LambdaLoggerHandler.__module__ = "awslambdaric.bootstrap"
        monkeypatch.setenv("OTEL_LOGS_EXPORTER", exporter)
        test_logger = logging.getLogger("test.runtime.handlers.disabled")
        runtime_handler = LambdaLoggerHandler(io.StringIO())
        test_logger.handlers = [runtime_handler]

        assert remove_lambda_runtime_stdout_handlers(test_logger) == 0
        assert test_logger.handlers == [runtime_handler]

    def test_keeps_awslambdaric_handlers_and_warns_when_handler_not_yet_defined(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """If ``configure_otel()`` runs above ``def lambda_handler``, stdout stays on so logs are not lost."""

        class LambdaLoggerHandler(logging.StreamHandler):  # type: ignore[type-arg]
            pass

        LambdaLoggerHandler.__module__ = "awslambdaric.bootstrap"
        monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")
        monkeypatch.setenv(ENV_KEEP_STDOUT_LOGS, "false")
        monkeypatch.setenv(
            "_HANDLER",
            "i_dot_ai_utilities.otel.__tests__.test_lambda_runtime.handler_not_defined_yet",
        )
        monkeypatch.delenv("ORIG_HANDLER", raising=False)
        runtime_handler = LambdaLoggerHandler(io.StringIO())
        root_logger = logging.getLogger()
        root_logger.addHandler(runtime_handler)
        try:
            LambdaRuntime(service_name="svc").on_reconfigure()
            assert runtime_handler in root_logger.handlers
            assert "call configure_otel() after defining the handler" in capsys.readouterr().err
        finally:
            root_logger.removeHandler(runtime_handler)

    def test_second_reconfigure_after_handler_definition_wraps_handler_and_removes_runtime_stdout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Calling ``configure_otel()`` again once ``def handler`` exists recovers wrapping and stdout opt-out."""

        class LambdaLoggerHandler(logging.StreamHandler):  # type: ignore[type-arg]
            pass

        LambdaLoggerHandler.__module__ = "awslambdaric.bootstrap"
        temp_mod = types.ModuleType("temp_late_lambda_mod")
        monkeypatch.setitem(sys.modules, "temp_late_lambda_mod", temp_mod)
        monkeypatch.setenv("OTEL_LOGS_EXPORTER", "otlp")
        monkeypatch.setenv(ENV_KEEP_STDOUT_LOGS, "false")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "2")
        monkeypatch.setenv(ENV_FLUSH_MARGIN_MS, "250")
        monkeypatch.setenv("_HANDLER", "temp_late_lambda_mod.handler")
        monkeypatch.delenv("ORIG_HANDLER", raising=False)

        runtime_handler = LambdaLoggerHandler(io.StringIO())
        root_logger = logging.getLogger()
        root_logger.addHandler(runtime_handler)
        runtime = LambdaRuntime(service_name="svc")
        delegate = ControllableLogExporter()
        processor = runtime.log_record_processor(delegate)
        try:
            runtime.on_reconfigure()
            assert runtime_handler in root_logger.handlers

            def handler(_event: dict[str, Any], context: Any = None) -> dict[str, int]:
                _ = context
                return {"statusCode": 200}

            temp_mod.handler = handler  # type: ignore[attr-defined]
            runtime.on_reconfigure()
            runtime.on_reconfigure()  # idempotent once wrapped
            assert runtime_handler not in root_logger.handlers

            class _LowTimeCtx:
                @staticmethod
                def get_remaining_time_in_millis() -> int:
                    return 100

            assert temp_mod.handler({}, context=_LowTimeCtx()) == {"statusCode": 200}
            assert not runtime.breaker.is_open()
        finally:
            uninstrument_installed_libraries()
            if runtime_handler in root_logger.handlers:
                root_logger.removeHandler(runtime_handler)
            processor.shutdown()


class TestLambdaFlushBudget:
    """Flushing yields to the handler response when the invocation is close to its Lambda timeout."""

    def test_skips_thread_and_breaker_when_no_records_are_queued(self) -> None:
        """Invocations that emit zero logs return immediately without spawning a flush thread or firing EMF."""
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=FakeClock())
        breaker.record_failure()
        delegate = ControllableLogExporter()
        processor = LambdaLogRecordProcessor(delegate, breaker)
        failures: list[str] = []
        try:
            assert (
                flush_lambda_logs(None, processor=processor, breaker=breaker, failure_emitter=failures.append) is True
            )
            assert failures == []
            assert delegate.batches == []

            processor.on_emit(ReadWriteLogRecord(LogRecord(body="queued")))
            assert (
                flush_lambda_logs(None, processor=processor, breaker=breaker, failure_emitter=failures.append) is False
            )
            assert failures == ["breaker_open"]
        finally:
            processor.shutdown()

    def test_skips_flush_without_opening_breaker_when_remaining_time_below_timeout_plus_margin(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "2")
        monkeypatch.setenv(ENV_FLUSH_MARGIN_MS, "250")

        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=FakeClock())
        delegate = ControllableLogExporter()
        processor = LambdaLogRecordProcessor(delegate, breaker)
        processor.on_emit(ReadWriteLogRecord(LogRecord(body="pending")))

        class _LowTimeCtx:
            @staticmethod
            def get_remaining_time_in_millis() -> int:
                return 2200  # below 2000 ms timeout + 250 ms margin

        failures: list[str] = []
        try:
            flushed = flush_lambda_logs(
                _LowTimeCtx(),
                processor=processor,
                breaker=breaker,
                failure_emitter=failures.append,
            )
            assert flushed is False
            assert processor.has_pending is True
            assert not breaker.is_open()
            assert failures == []
        finally:
            processor.shutdown()

    def test_flush_failure_without_exporter_error_opens_breaker_and_emits_flush_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=FakeClock())
        assert flush_lambda_logs(None, processor=None, breaker=breaker) is True

        delegate = ControllableLogExporter()
        processor = LambdaLogRecordProcessor(delegate, breaker)
        processor.on_emit(ReadWriteLogRecord(LogRecord(body="pending")))
        failures: list[str] = []
        try:

            class _FalseFlushProvider:
                @staticmethod
                def force_flush(_timeout_millis: int) -> bool:
                    return False

            monkeypatch.setattr(_lambda, "get_logger_provider", _FalseFlushProvider)
            assert (
                flush_lambda_logs(None, processor=processor, breaker=breaker, failure_emitter=failures.append) is False
            )
            assert breaker.is_open()
            assert failures == ["flush_failure"]

            breaker.record_success()
            failures.clear()

            class _RaisingFlushProvider:
                @staticmethod
                def force_flush(_timeout_millis: int) -> bool:
                    msg = "provider flush failed"
                    raise RuntimeError(msg)

            monkeypatch.setattr(_lambda, "get_logger_provider", _RaisingFlushProvider)
            assert (
                flush_lambda_logs(None, processor=processor, breaker=breaker, failure_emitter=failures.append) is False
            )
            assert breaker.is_open()
            assert failures == ["flush_failure"]

            breaker.record_success()
            monkeypatch.setattr(_lambda, "get_logger_provider", object)
            assert (
                flush_lambda_logs(None, processor=processor, breaker=breaker, failure_emitter=failures.append) is True
            )
            assert [rec.log_record.body for rec in delegate.batches[0]] == ["pending"]
        finally:
            processor.shutdown()

    def test_flushes_normally_when_context_remaining_time_raises_or_returns_non_number(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(_lambda, "get_logger_provider", object)
        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=FakeClock())
        delegate = ControllableLogExporter()
        processor = LambdaLogRecordProcessor(delegate, breaker)

        class _BrokenCtx:
            @staticmethod
            def get_remaining_time_in_millis() -> int:
                msg = "context unavailable"
                raise RuntimeError(msg)

        class _StringTimeCtx:
            @staticmethod
            def get_remaining_time_in_millis() -> str:
                return "not-a-number"

        try:
            processor.on_emit(ReadWriteLogRecord(LogRecord(body="first")))
            assert flush_lambda_logs(_BrokenCtx(), processor=processor, breaker=breaker) is True
            processor.on_emit(ReadWriteLogRecord(LogRecord(body="second")))
            assert flush_lambda_logs(_StringTimeCtx(), processor=processor, breaker=breaker) is True
            assert [[r.log_record.body for r in batch] for batch in delegate.batches] == [["first"], ["second"]]
        finally:
            processor.shutdown()

    def test_flush_timeout_opens_circuit_breaker_and_join_buffer_avoids_spurious_timeout(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A worker thread that exceeds the join cap opens the breaker, while minor overhead completes cleanly."""
        import threading  # noqa: PLC0415
        import time  # noqa: PLC0415

        monkeypatch.setattr(_lambda, "get_logger_provider", object)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "0.05")
        monkeypatch.setenv(ENV_FLUSH_MARGIN_MS, "250")

        gate = threading.Event()

        class _BlockingExporter(ControllableLogExporter):
            def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
                gate.wait(timeout=2.0)
                return super().export(batch)

        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=FakeClock())
        delegate = _BlockingExporter()
        processor = LambdaLogRecordProcessor(delegate, breaker)
        failures: list[str] = []
        try:
            processor.on_emit(ReadWriteLogRecord(LogRecord(body="stuck")))
            assert (
                flush_lambda_logs(None, processor=processor, breaker=breaker, failure_emitter=failures.append) is False
            )
            assert failures == ["flush_timeout"]
            assert breaker.is_open()
        finally:
            gate.set()
            if _lambda._inflight_flush is not None:  # noqa: SLF001
                _lambda._inflight_flush.join(timeout=1.0)  # noqa: SLF001
            _lambda.reset_inflight_flush()
            processor.shutdown()

        # Export taking slightly longer than the 50 ms HTTP timeout still completes within the 100 ms join buffer.
        class _SlightlySlowExporter(ControllableLogExporter):
            def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
                time.sleep(0.07)
                return super().export(batch)

        breaker_ok = ExportCircuitBreaker(open_seconds=30.0, clock=FakeClock())
        delegate_ok = _SlightlySlowExporter()
        processor_ok = LambdaLogRecordProcessor(delegate_ok, breaker_ok)
        failures_ok: list[str] = []
        try:
            processor_ok.on_emit(ReadWriteLogRecord(LogRecord(body="within-join-buffer")))
            assert (
                flush_lambda_logs(None, processor=processor_ok, breaker=breaker_ok, failure_emitter=failures_ok.append)
                is True
            )
            assert failures_ok == []
            assert not breaker_ok.is_open()
            assert [rec.log_record.body for rec in delegate_ok.batches[0]] == ["within-join-buffer"]
        finally:
            processor_ok.shutdown()

    def test_healthy_multi_batch_deadline_stop_keeps_breaker_closed_and_stays_quiet(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Stopping between healthy batches on deadline leaves the backlog queued without tripping the breaker."""
        monkeypatch.setattr(_lambda, "get_logger_provider", object)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", "0.3")
        monkeypatch.setenv("OTEL_BLRP_MAX_EXPORT_BATCH_SIZE", "2")

        clock = FakeClock()

        class _SlowHealthyExporter(ControllableLogExporter):
            def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
                clock.advance(0.11)
                return super().export(batch)

        breaker = ExportCircuitBreaker(open_seconds=30.0, clock=clock)
        delegate = _SlowHealthyExporter()
        processor = LambdaLogRecordProcessor(delegate, breaker, clock=clock)
        failures: list[str] = []
        try:
            for i in range(8):
                processor.on_emit(ReadWriteLogRecord(LogRecord(body=f"r{i}")))

            assert (
                flush_lambda_logs(None, processor=processor, breaker=breaker, failure_emitter=failures.append) is False
            )
            assert len(delegate.batches) == 3
            assert processor.has_pending is True
            assert not breaker.is_open()
            assert failures == []

            # Next invocation drains the remaining batch cleanly without hitting breaker_open.
            assert (
                flush_lambda_logs(None, processor=processor, breaker=breaker, failure_emitter=failures.append) is True
            )
            assert len(delegate.batches) == 4
            assert processor.has_pending is False
            assert failures == []
        finally:
            processor.shutdown()


class TestLambdaEndToEnd:
    """Lambda freezes the process as soon as the handler returns, so logs flush synchronously per invocation."""

    def test_exports_trace_correlated_logs_and_traces_with_lambda_resource_and_headers_without_metrics(
        self,
    ) -> None:
        """``otel-gateway`` runs logs and traces pipelines; metrics remain ``none``."""
        with OtlpCaptureServer() as server:
            proc, summary = _run_scenario(
                [
                    {
                        "request_id": "req-happy-1",
                        "remaining_ms": 4500,
                        "event": {
                            "mode": "ok",
                            "application": "casework",
                            "emit_internal_otel_log": True,
                            "extra_attributes": {"raw_tag": "unnamespaced-val"},
                        },
                    }
                ],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_EXPORTER_OTLP_HEADERS": "x-custom-test=iai-test",
                    "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=dev",
                    "TEST_EXPLICIT_SERVICE_NAME": "core-auth-api",
                    "TEST_CALL_CONFIGURE_TWICE": "1",
                },
            )

            assert len(server.requests_for_path("/v1/logs")) == 1
            assert len(server.requests_for_path("/v1/traces")) == 1
            assert server.requests_for_path("/v1/metrics") == []

            req = server.requests_for_path("/v1/logs")[0]
            assert req.headers.get("x-custom-test") == "iai-test"
            assert req.headers.get("content-encoding") == "gzip"
            assert len(req.records) == 1

            record = req.records[0]
            ret = summary["results"][0]["returned"]
            assert ret["statusCode"] == 200
            assert record.body == "Authorisation request started for casework"
            assert record.severity_text == "INFO"
            assert record.trace_id == ret["trace_id"]
            assert record.span_id == ret["span_id"]
            assert len(record.trace_id) == 32
            assert record.timestamp > 0

            res_attrs = record.resource_attributes
            assert res_attrs["service.name"] == "core-auth-api"
            assert res_attrs["deployment.environment.name"] == "dev"
            assert res_attrs["cloud.provider"] == "aws"
            assert res_attrs["cloud.platform"] == "aws_lambda"
            assert res_attrs["cloud.region"] == "eu-west-2"
            assert res_attrs["faas.name"] == "core-auth-api-authorise-token"
            assert res_attrs["faas.version"] == "$LATEST"
            assert res_attrs["faas.instance"] == "2026/10/16/[$LATEST]abcdef"
            assert res_attrs["faas.max_memory"] == 512

            attrs = record.attributes
            assert attrs["i_ai.auth.application"] == "casework"
            assert attrs["i_ai.unnamespaced.raw_tag"] == "unnamespaced-val"
            assert "raw_tag" not in attrs
            assert proc.stderr.count("log attribute 'raw_tag' is not an OpenTelemetry semantic convention") == 1
            assert "code.file.path" in attrs
            assert attrs["code.function.name"] == "lambda_handler"
            assert isinstance(attrs["code.line.number"], int)

    def test_never_copies_sensitive_event_headers_or_jwt_body_onto_exported_logs(self) -> None:
        """Lambda events often carry bearer tokens and JWT payloads; auto-instrumentation must not attach them."""
        with OtlpCaptureServer() as server:
            _run_scenario(
                [
                    {
                        "request_id": "req-privacy",
                        "remaining_ms": 4500,
                        "event": {
                            "mode": "ok",
                            "application": "casework",
                            "headers": {"Authorization": "Bearer secret-jwt-token"},
                            "body": '{"token": "eyJhbGciOiJSUzI1NiJ9.eyJlbWFpbCI6InRlc3RAZXhhbXBsZS5jb20ifQ.sig"}',
                        },
                    }
                ],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "core-auth-api",
                },
            )

            assert len(server.log_records) == 1
            serialized_attrs = json.dumps(server.log_records[0].attributes)
            assert "secret-jwt-token" not in serialized_attrs
            assert "eyJhbGciOiJSUzI1NiJ9" not in serialized_attrs
            assert "authorization" not in serialized_attrs.lower()

    def test_stdout_opt_out_suppresses_runtime_stdout_while_preserving_custom_app_handlers(self) -> None:
        with OtlpCaptureServer() as server:
            proc, summary = _run_scenario(
                [
                    {
                        "request_id": "req-opt-out",
                        "remaining_ms": 4500,
                        "event": {"mode": "ok", "application": "casework"},
                    }
                ],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "core-auth-api",
                    "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false",
                    "TEST_INSTALL_USER_HANDLER": "1",
                },
            )

            assert proc.stdout == ""
            assert summary["runtime_handler_output"] == ""
            assert "Authorisation request started for casework" in summary["user_handler_output"]
            assert [rec.body for rec in server.log_records] == ["Authorisation request started for casework"]

    def test_default_keeps_both_runtime_stdout_and_otlp_export(self) -> None:
        with OtlpCaptureServer() as server:
            _, summary = _run_scenario(
                [{"request_id": "req-both", "remaining_ms": 4500, "event": {"mode": "ok", "application": "dual"}}],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "core-auth-api",
                    "ENVIRONMENT": "preprod",
                },
            )
            assert server.log_records[0].resource_attributes["deployment.environment.name"] == "preprod"
            assert "Authorisation request started for dual" in summary["runtime_handler_output"]
            assert [rec.body for rec in server.log_records] == ["Authorisation request started for dual"]

    def test_flushes_logs_on_handled_error_and_unhandled_exception_preserving_traceback(self) -> None:
        with OtlpCaptureServer() as server:
            proc, summary = _run_scenario(
                [
                    {
                        "request_id": "req-caught",
                        "remaining_ms": 4500,
                        "event": {"mode": "caught_error", "application": "app-a"},
                    },
                    {
                        "request_id": "req-raise",
                        "remaining_ms": 4500,
                        "event": {"mode": "raise", "application": "app-b"},
                    },
                ],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "core-auth-api",
                },
            )

            assert proc.stdout == ""
            first, second = summary["results"]
            assert first["returned"]["statusCode"] == 400
            assert second["raised"] == "ValueError"
            assert second["message"] == "synthetic handler failure"
            assert "synthetic handler failure" in second["traceback"]

            assert len(server.requests_for_path("/v1/logs")) == 2
            bodies = [rec.body for rec in server.log_records]
            assert "Input validation failed" in bodies
            assert "Authorisation request failed" in bodies

            exc_records = [rec for rec in server.log_records if rec.body == "Authorisation request failed"]
            assert len(exc_records) == 1
            exc_attrs = exc_records[0].attributes
            assert exc_attrs["exception.type"] == "ValueError"
            assert exc_attrs["exception.message"] == "synthetic handler failure"
            assert "ValueError: synthetic handler failure" in exc_attrs["exception.stacktrace"]

    def test_batches_multiple_logs_from_single_invocation_into_one_otlp_post(self) -> None:
        with OtlpCaptureServer() as server:
            proc, _ = _run_scenario(
                [
                    {
                        "request_id": "req-burst",
                        "remaining_ms": 4500,
                        "event": {"mode": "batch_burst", "count": 8},
                    }
                ],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "core-auth-api",
                },
            )

            assert proc.stdout == ""
            assert len(server.requests_for_path("/v1/logs")) == 1
            assert len(server.log_records) == 9  # 1 start log + 8 burst logs

    def test_skips_flush_when_remaining_time_is_tight_and_drains_on_next_invocation(self) -> None:
        with OtlpCaptureServer() as server:
            proc, _ = _run_scenario(
                [
                    {
                        "request_id": "req-tight",
                        "remaining_ms": 1500,  # below 2000 ms timeout + 250 ms margin
                        "event": {"mode": "ok", "application": "first-invoke"},
                    },
                    {
                        "request_id": "req-ample",
                        "remaining_ms": 4500,
                        "event": {"mode": "ok", "application": "second-invoke"},
                    },
                ],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "core-auth-api",
                },
            )

            assert proc.stdout == ""
            assert len(server.requests_for_path("/v1/logs")) == 1
            apps = [rec.attributes.get("i_ai.auth.application") for rec in server.log_records]
            assert apps == ["first-invoke", "second-invoke"]

    def test_gateway_503_opens_circuit_breaker_and_emits_emf_for_failed_and_skipped_flushes(self) -> None:
        """Skipped flushes still emit ``ExportFailures=1`` so the CloudWatch alarm stays in ALARM for the outage."""
        with OtlpCaptureServer(status_code=503, delay_seconds=0.35) as server:
            proc, summary = _run_scenario(
                [
                    {
                        "request_id": "req-fail-1",
                        "remaining_ms": 4500,
                        "event": {"mode": "ok", "application": "outage-1"},
                    },
                    {
                        "request_id": "req-fail-2",
                        "remaining_ms": 4500,
                        "event": {"mode": "ok", "application": "outage-2"},
                    },
                ],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "core-auth-api",
                    "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT": "0.5",
                    "I_DOT_AI_OTEL_LAMBDA_FLUSH_MARGIN_MS": "100",
                    "I_DOT_AI_OTEL_LAMBDA_CIRCUIT_BREAKER_SECONDS": "30",
                },
            )

            first, second = summary["results"]
            assert first["elapsed_ms"] < 1500.0
            assert second["elapsed_ms"] < 200.0
            assert len(server.requests_for_path("/v1/logs")) == 1

            stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
            assert len(stdout_lines) == 2
            emf, breaker_emf = (json.loads(line) for line in stdout_lines)
            assert emf["level"] == "WARNING"
            assert emf["event"] == EMF_EVENT_NAME
            assert emf["reason"] == "export_failure"
            assert emf["FunctionName"] == "core-auth-api-authorise-token"
            assert emf[EMF_METRIC_NAME] == 1
            assert emf["_aws"]["CloudWatchMetrics"][0]["Namespace"] == EMF_NAMESPACE
            assert breaker_emf["reason"] == "breaker_open"

    def test_slow_dns_is_bounded_by_export_timeout_and_skips_while_previous_flush_runs(self) -> None:
        """``socket.getaddrinfo`` ignores HTTP client timeouts; the worker thread bounds handler latency."""
        proc, summary = _run_scenario(
            [
                {"request_id": "req-dns-1", "remaining_ms": 4500, "event": {"mode": "ok"}},
                {"request_id": "req-dns-2", "remaining_ms": 4500, "event": {"mode": "ok"}},
            ],
            env_overrides={
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://otel-gateway.invalid:4318",
                "OTEL_SERVICE_NAME": "core-auth-api",
                "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT": "0.5",
                "I_DOT_AI_OTEL_LAMBDA_FLUSH_MARGIN_MS": "100",
                "TEST_SLOW_DNS_SECONDS": "5",
            },
        )

        first, second = summary["results"]
        assert first["returned"]["statusCode"] == 200
        assert second["returned"]["statusCode"] == 200
        assert first["elapsed_ms"] < 1000.0
        assert second["elapsed_ms"] < 200.0
        reasons = [json.loads(line)["reason"] for line in proc.stdout.splitlines() if line.strip()]
        assert reasons == ["flush_timeout", "flush_in_progress"]

    def test_flushes_otlp_when_another_exporter_is_listed_first(self) -> None:
        with OtlpCaptureServer() as server:
            _run_scenario(
                [{"request_id": "req-two", "remaining_ms": 4500, "event": {"mode": "ok", "application": "two"}}],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "core-auth-api",
                    "OTEL_LOGS_EXPORTER": "console,otlp",
                },
            )
            assert [rec.body for rec in server.log_records] == ["Authorisation request started for two"]

    def test_logs_exporter_none_keeps_runtime_stdout_logging(self) -> None:
        _, summary = _run_scenario(
            [{"request_id": "req-off", "remaining_ms": 4500, "event": {"mode": "ok", "application": "off"}}],
            env_overrides={
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:9",
                "OTEL_SERVICE_NAME": "core-auth-api",
                "OTEL_LOGS_EXPORTER": "none",
            },
        )
        assert "Authorisation request started for off" in summary["runtime_handler_output"]

    def test_missing_endpoint_warns_once_and_keeps_runtime_stdout_logging(self) -> None:
        proc, summary = _run_scenario(
            [{"request_id": "req-no-endpoint", "remaining_ms": 4500, "event": {"mode": "ok"}}],
            env_overrides={
                "OTEL_SERVICE_NAME": "core-auth-api",
                "TEST_CALL_CONFIGURE_TWICE": "1",
            },
        )
        assert proc.stdout == ""
        assert "Authorisation request started" in summary["runtime_handler_output"]
        assert proc.stderr.count("continuing with OTLP log export disabled") == 1
