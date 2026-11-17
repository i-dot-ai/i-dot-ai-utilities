# ruff: noqa: SLF001 - these tests deliberately inspect private state and SDK internals.
"""Unit tests for attribute naming rules, the container runtime helpers, stdout JSON, and SDK internals."""

from __future__ import annotations

import importlib
import io
import json
import logging
import pkgutil
import signal

import pytest
from opentelemetry._logs import LogRecord
from opentelemetry.sdk._logs import LoggerProvider, ReadWriteLogRecord
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    InMemoryLogRecordExporter,
    LogRecordExportResult,
    SimpleLogRecordProcessor,
)

from i_dot_ai_utilities.otel import I_AI_NAMESPACE, iai_attribute, validate_attributes
from i_dot_ai_utilities.otel._attributes import (
    ENV_STRICT_ATTRIBUTES,
    AttributeNamespaceProcessor,
    _semconv_registry,
    is_valid_attribute_name,
)
from i_dot_ai_utilities.otel._ecs import (
    batch_resource_attributes,
    install_sigterm_handler,
    register_capped_shutdown,
)
from i_dot_ai_utilities.otel._emf import RateLimitedFailureSignal
from i_dot_ai_utilities.otel._environment import (
    ENV_KEEP_STDOUT_LOGS,
    ENV_KEEP_STDOUT_LOGS_LAMBDA,
    keep_stdout_logs,
)
from i_dot_ai_utilities.otel._export import FailureReportingExporter
from i_dot_ai_utilities.otel._instrumentation import disabled_instrumentations
from i_dot_ai_utilities.otel._stdout import JsonLineFormatter


class TestAttributeNames:
    @pytest.mark.parametrize(
        "name",
        [
            "i_ai.auth.decision.reason",
            "i_ai.auth.token.key_id",
            "i_ai.auth.application",
            "user.email",
            "url.full",
            "error.type",
            "code.file.path",
            "exception.stacktrace",
            "aws.ecs.task.arn",
            "http.request.header.x_request_id",
        ],
    )
    def test_valid_names(self, name: str) -> None:
        assert is_valid_attribute_name(name)

    @pytest.mark.parametrize(
        "name",
        [
            "application",
            "user.my_field",
            "i_ai.auth",
            "i_ai.auth.tokenKeyId",
            "i_ai.Auth.reason",
            "i_ai..auth.reason",
            "iai.auth.reason",
            "http.request.header.",
        ],
    )
    def test_invalid_names(self, name: str) -> None:
        assert not is_valid_attribute_name(name)

    def test_iai_attribute_builds_and_validates(self) -> None:
        assert I_AI_NAMESPACE == "i_ai"
        assert iai_attribute("auth.decision.reason") == "i_ai.auth.decision.reason"
        for bad in ("auth", "Auth.reason", "auth.tokenKeyId", "auth..reason", ""):
            with pytest.raises(ValueError, match="Invalid i_ai attribute"):
                iai_attribute(bad)

    def test_validate_attributes_lists_every_bad_key(self) -> None:
        assert validate_attributes({"user.email": "a@b.c"}) == {"user.email": "a@b.c"}
        with pytest.raises(ValueError, match=r"\['application', 'user.my_field'\]"):
            validate_attributes({"user.my_field": 1, "application": "x", "url.full": "y"})

    def test_registry_parsing_matches_importing_every_module(self) -> None:
        # The registry is read from source for start-up speed; this fails if a semconv
        # release changes the constant format the regex relies on.
        imported: set[str] = set()
        for package_name in ("opentelemetry.semconv.attributes", "opentelemetry.semconv._incubating.attributes"):
            package = importlib.import_module(package_name)
            for module_info in pkgutil.iter_modules(package.__path__):
                module = importlib.import_module(f"{package_name}.{module_info.name}")
                imported |= {v for k, v in vars(module).items() if k.isupper() and isinstance(v, str)}
        names, templates = _semconv_registry()
        assert len(names) > 500
        assert names | {t.removesuffix(".") for t in templates} == imported


class TestAttributeNamespaceProcessor:
    @staticmethod
    def _emit(processor: AttributeNamespaceProcessor, attributes: dict[str, object]) -> ReadWriteLogRecord:
        record = ReadWriteLogRecord(LogRecord(body="m", attributes=attributes))  # type: ignore[arg-type]
        processor.on_emit(record)
        return record

    def test_renames_bad_keys_and_warns_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        exporter = InMemoryLogRecordExporter()
        processor = AttributeNamespaceProcessor(SimpleLogRecordProcessor(exporter))
        self._emit(processor, {"mode": "ok", "i_ai.auth.application": "x", "code.line.number": 3})
        self._emit(processor, {"mode": "again"})

        exported = [dict(r.log_record.attributes or {}) for r in exporter.get_finished_logs()]
        assert exported == [
            {"i_ai.auth.application": "x", "code.line.number": 3, "i_ai.unnamespaced.mode": "ok"},
            {"i_ai.unnamespaced.mode": "again"},
        ]
        assert capsys.readouterr().err.count("log attribute 'mode'") == 1

    def test_strict_mode_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(ENV_STRICT_ATTRIBUTES, "true")
        processor = AttributeNamespaceProcessor(SimpleLogRecordProcessor(InMemoryLogRecordExporter()))
        with pytest.raises(ValueError, match="application"):
            self._emit(processor, {"application": "x"})


class TestContainerHelpers:
    def test_batch_attributes_only_for_set_variables(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("AWS_BATCH_JOB_ID", "AWS_BATCH_JOB_ATTEMPT", "AWS_BATCH_JQ_NAME"):
            monkeypatch.delenv(name, raising=False)
        assert batch_resource_attributes() == {}
        monkeypatch.setenv("AWS_BATCH_JOB_ID", "job-1")
        monkeypatch.setenv("AWS_BATCH_JQ_NAME", " ")
        assert batch_resource_attributes() == {"i_ai.aws.batch.job.id": "job-1"}

    def test_keep_stdout_new_name_wins_and_old_name_still_works(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(ENV_KEEP_STDOUT_LOGS, raising=False)
        monkeypatch.delenv(ENV_KEEP_STDOUT_LOGS_LAMBDA, raising=False)
        assert keep_stdout_logs(default=True) is True
        assert keep_stdout_logs(default=False) is False
        monkeypatch.setenv(ENV_KEEP_STDOUT_LOGS_LAMBDA, "true")
        assert keep_stdout_logs(default=False) is True
        monkeypatch.setenv(ENV_KEEP_STDOUT_LOGS, "false")
        assert keep_stdout_logs(default=True) is False

    def test_sigterm_handler_installed_only_when_default(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            assert install_sigterm_handler() is True
            assert install_sigterm_handler() is False

            def existing(*_: object) -> None: ...

            signal.signal(signal.SIGTERM, existing)
            assert install_sigterm_handler() is False
            assert signal.getsignal(signal.SIGTERM) is existing
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_capped_shutdown_replaces_sdk_atexit(self) -> None:
        provider = LoggerProvider()
        assert provider._at_exit_handler is not None
        register_capped_shutdown(provider, 0.1)
        assert provider._at_exit_handler is None
        provider.shutdown()

    def test_failure_reporting_exporter(self) -> None:
        reasons: list[str] = []

        class _Failing(InMemoryLogRecordExporter):
            def export(self, _batch: object) -> LogRecordExportResult:  # type: ignore[override]
                raise RuntimeError

        assert FailureReportingExporter(_Failing(), reasons.append).export([]) is LogRecordExportResult.FAILURE
        assert FailureReportingExporter(InMemoryLogRecordExporter(), reasons.append).export([]) is (
            LogRecordExportResult.SUCCESS
        )
        assert reasons == ["export_failure"]

    def test_rate_limited_failure_signal(self, capsys: pytest.CaptureFixture[str]) -> None:
        signal_ = RateLimitedFailureSignal(dimension="ServiceName", value="svc", interval_seconds=60)
        signal_("export_failure")
        signal_("export_failure")
        lines = capsys.readouterr().out.splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["ServiceName"] == "svc"

    def test_disabled_instrumentations_parsing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", " fastapi , botocore,,")
        assert disabled_instrumentations() == {"fastapi", "botocore"}


class TestJsonLineFormatter:
    def test_single_line_with_extras_and_exception(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter())
        test_logger = logging.getLogger("test.json")
        test_logger.propagate = False
        test_logger.addHandler(handler)
        error = ValueError("boom\nsecond line")
        try:
            test_logger.error(
                "failed %s",
                "op",
                exc_info=(ValueError, error, None),
                extra={"i_ai.sample.item.id": 7},
            )
        finally:
            test_logger.removeHandler(handler)

        lines = stream.getvalue().splitlines()
        assert len(lines) == 1
        payload = json.loads(lines[0])
        assert payload["message"] == "failed op"
        assert payload["level"] == "ERROR"
        assert payload["logger"] == "test.json"
        assert payload["i_ai.sample.item.id"] == 7
        assert "ValueError: boom" in payload["exception"]
        assert "msg" not in payload
        assert "args" not in payload


class TestSdkInternals:
    """Fail loudly when an SDK upgrade removes an internal this package relies on."""

    def test_batch_processor_queue_exists(self) -> None:
        processor = BatchLogRecordProcessor(InMemoryLogRecordExporter())
        try:
            assert hasattr(processor._batch_processor._queue, "appendleft")
        finally:
            processor.shutdown()

    def test_logger_provider_atexit_handler_exists(self) -> None:
        provider = LoggerProvider()
        try:
            assert callable(provider._at_exit_handler)
        finally:
            provider.shutdown()
