# ruff: noqa: SLF001 - these tests deliberately inspect private configuration helpers and pinned SDK internals.
"""Tests for ``configure_otel``, runtime detection, service-name rules, root logging, and import boundaries."""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)

import i_dot_ai_utilities.otel as public_otel
from i_dot_ai_utilities.otel.__tests__.fixtures.otlp_capture_server import OtlpCaptureServer
from i_dot_ai_utilities.otel._configuration import (
    _ExcludeInternalOtelFilter,
    _install_root_handler,
    configure_otel,
    detect_runtime,
)
from i_dot_ai_utilities.otel._instrumentation import disabled_instrumentations


def _clean_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return a copy of ``os.environ`` without OTel or AWS runtime variables that would skew detection."""
    stripped_prefixes = ("OTEL_", "I_DOT_AI_OTEL_", "AWS_LAMBDA_", "AWS_BATCH_", "ECS_", "ENVIRONMENT")
    env = {key: value for key, value in os.environ.items() if not key.startswith(stripped_prefixes)}
    if extra:
        env.update(extra)
    return env


def _run_python(script: str, *, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        env=_clean_subprocess_env(env_overrides),
        check=False,
    )
    assert proc.returncode == 0, f"subprocess failed: stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return proc


class TestPublicSurface:
    """``i_dot_ai_utilities.otel`` exposes a small, stable entry-point API."""

    def test_exports_documented_public_api(self) -> None:
        assert public_otel.__all__ == [
            "I_AI_NAMESPACE",
            "configure_otel",
            "flush_otel",
            "iai_attribute",
            "validate_attributes",
        ]
        assert public_otel.configure_otel is configure_otel


class TestImportBoundaries:
    """Optional extras keep OpenTelemetry out of base installs and Lambda dependencies out of containers."""

    _BLOCK_LAMBDA_INSTRUMENTORS = (
        "import sys\n"
        "blocked = {\n"
        "    'opentelemetry.instrumentation.aws_lambda',\n"
        "    'opentelemetry.instrumentation.botocore',\n"
        "}\n"
        "class _BlockLambdaInst:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name in blocked:\n"
        "            raise ImportError('missing ' + name)\n"
        "        return None\n"
        "sys.meta_path.insert(0, _BlockLambdaInst())\n"
    )

    def test_base_package_and_structured_logger_import_without_opentelemetry(self) -> None:
        script = (
            "import sys\n"
            "class _BlockOtel:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name == 'opentelemetry' or name.startswith('opentelemetry.'):\n"
            "            raise ImportError('blocked ' + name)\n"
            "        return None\n"
            "sys.meta_path.insert(0, _BlockOtel())\n"
            "import i_dot_ai_utilities\n"
            "from i_dot_ai_utilities.logging.structured_logger import StructuredLogger\n"
            "try:\n"
            "    import i_dot_ai_utilities.otel\n"
            "except ImportError:\n"
            "    print('OTEL_IMPORT_BLOCKED_OK')\n"
        )
        proc = _run_python(script)
        assert "OTEL_IMPORT_BLOCKED_OK" in proc.stdout

    def test_otel_package_does_not_import_structlog(self) -> None:
        """``i_dot_ai_utilities.otel`` uses standard ``logging`` and stays decoupled from ``StructuredLogger``."""
        script = (
            "import sys\n"
            "import i_dot_ai_utilities.otel\n"
            "assert 'structlog' not in sys.modules\n"
            "print('NO_STRUCTLOG_OK')\n"
        )
        proc = _run_python(script)
        assert "NO_STRUCTLOG_OK" in proc.stdout

    def test_local_runtime_configures_without_lambda_instrumentors(self) -> None:
        """Container and local installs use ``[otel_native]`` without pulling in Lambda packages."""
        script = (
            self._BLOCK_LAMBDA_INSTRUMENTORS
            + "from i_dot_ai_utilities.otel import configure_otel\n"
            + "configure_otel(service_name='container-svc', runtime='local')\n"
            + "print('LOCAL_OK')\n"
        )
        proc = _run_python(script)
        assert "LOCAL_OK" in proc.stdout

    def test_lambda_runtime_fails_fast_when_lambda_instrumentor_missing(self) -> None:
        """A Lambda deployed without ``[otel_native_lambda]`` raises ``ImportError`` at start-up."""
        script = (
            self._BLOCK_LAMBDA_INSTRUMENTORS
            + "from i_dot_ai_utilities.otel import configure_otel\n"
            + "try:\n"
            + "    configure_otel(service_name='lambda-svc', runtime='lambda')\n"
            + "except ImportError:\n"
            + "    print('LAMBDA_IMPORT_ERROR_OK')\n"
        )
        proc = _run_python(script)
        assert "LAMBDA_IMPORT_ERROR_OK" in proc.stdout


class TestRuntimeDetection:
    """``detect_runtime`` checks explicit overrides first, then Lambda, then ECS or Batch, then local."""

    _RUNTIME_VARS = ("AWS_LAMBDA_FUNCTION_NAME", "ECS_CONTAINER_METADATA_URI_V4", "AWS_BATCH_JOB_ID")

    def test_defaults_to_local_when_no_aws_env_vars_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in self._RUNTIME_VARS:
            monkeypatch.delenv(name, raising=False)
        assert detect_runtime() == "local"

    def test_detects_ecs_from_container_metadata_uri_or_batch_job_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for name in self._RUNTIME_VARS:
            monkeypatch.delenv(name, raising=False)

        monkeypatch.setenv("AWS_BATCH_JOB_ID", "job-1")
        assert detect_runtime() == "ecs"

        monkeypatch.delenv("AWS_BATCH_JOB_ID")
        monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
        assert detect_runtime() == "ecs"

    def test_prefers_lambda_over_ecs_when_both_env_vars_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ECS_CONTAINER_METADATA_URI_V4", "http://169.254.170.2/v4/abc")
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
        assert detect_runtime() == "lambda"

    def test_explicit_runtime_overrides_environment_and_rejects_unknown_value(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("AWS_LAMBDA_FUNCTION_NAME", "my-fn")
        assert detect_runtime("local") == "local"
        assert detect_runtime("ecs") == "ecs"

        monkeypatch.delenv("AWS_LAMBDA_FUNCTION_NAME")
        assert detect_runtime("lambda") == "lambda"

        with pytest.raises(ValueError, match="Unsupported runtime"):
            detect_runtime("invalid")  # type: ignore[arg-type]


class TestServiceNameResolution:
    """``otel-gateway`` drops logs whose ``service.name`` is not on its allow-list while still returning HTTP 200.

    Shared Terraform modules set ``OTEL_SERVICE_NAME`` to the allow-listed name, so the environment must win over
    any fallback passed in code, and missing ``service.name`` must disable OTLP export rather than send the SDK's
    ``unknown_service`` default into a black hole.
    """

    def test_env_service_name_overrides_explicit_argument(self) -> None:
        script = (
            "import logging\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "provider = configure_otel(service_name='explicit-service-name')\n"
            "logging.getLogger('app').info('hello')\n"
            "provider.force_flush()\n"
        )
        with OtlpCaptureServer() as server:
            _run_python(
                script,
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "env-service-name",
                },
            )
            assert server.log_records[0].resource_attributes["service.name"] == "env-service-name"

    def test_explicit_service_name_used_as_fallback_when_env_unset(self) -> None:
        script = (
            "import logging\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "provider = configure_otel(service_name='explicit-service-name')\n"
            "logging.getLogger('app').info('hello')\n"
            "provider.force_flush()\n"
        )
        with OtlpCaptureServer() as server:
            _run_python(
                script,
                env_overrides={"OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url},
            )
            assert server.log_records[0].resource_attributes["service.name"] == "explicit-service-name"

    def test_missing_service_name_disables_otlp_export_warns_once_and_keeps_stdout(self) -> None:
        script = (
            "import logging\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "configure_otel()\n"
            "provider = configure_otel()\n"
            "logging.getLogger('app').info('kept-on-stdout')\n"
            "provider.force_flush()\n"
        )
        with OtlpCaptureServer() as server:
            proc = _run_python(
                script,
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false",
                },
            )
            assert server.requests == []
            assert proc.stderr.count("service_name is not set") == 1
            (stdout_line,) = proc.stdout.splitlines()
            assert json.loads(stdout_line)["message"] == "kept-on-stdout"


class TestRootLoggerSetup:
    """The root OpenTelemetry handler prevents exporter recursion, quiets noisy SDKs, and survives dictConfig."""

    def test_excludes_internal_opentelemetry_logs_to_prevent_exporter_recursion(self) -> None:
        """Exporter failures log under ``opentelemetry.*``; re-exporting them would loop indefinitely."""
        otel_filter = _ExcludeInternalOtelFilter()
        internal_rec = logging.LogRecord("opentelemetry.sdk", logging.WARNING, "f", 1, "m", (), None)
        root_otel_rec = logging.LogRecord("opentelemetry", logging.WARNING, "f", 1, "m", (), None)
        app_rec = logging.LogRecord("api.handlers.authorise_token", logging.INFO, "f", 1, "m", (), None)

        assert otel_filter.filter(internal_rec) is False
        assert otel_filter.filter(root_otel_rec) is False
        assert otel_filter.filter(app_rec) is True

    def test_quiets_botocore_boto3_and_urllib3_to_warning_unless_app_set_level(self) -> None:
        """AWS SDK and HTTP client INFO chatter stays out of OTLP unless the application opted in."""
        names = ("botocore", "boto3", "urllib3")
        saved = {name: logging.getLogger(name).level for name in (*names, "")}
        root_logger = logging.getLogger()
        exporter = InMemoryLogRecordExporter()
        provider = LoggerProvider()
        provider.add_log_record_processor(SimpleLogRecordProcessor(exporter))
        for name in names:
            logging.getLogger(name).setLevel(logging.NOTSET)
        logging.getLogger("urllib3").setLevel(logging.INFO)
        handler = _install_root_handler(provider)
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

    def test_otlp_handler_survives_dictconfig_reconfiguration(self) -> None:
        """Applications and tools like Alembic call ``dictConfig`` after start-up, which replaces root handlers."""
        script = (
            "import logging, logging.config\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "provider = configure_otel(service_name='reconfigured-svc')\n"
            "logging.config.dictConfig({'version': 1, 'disable_existing_loggers': False,\n"
            "    'handlers': {'console': {'class': 'logging.StreamHandler'}},\n"
            "    'root': {'handlers': ['console'], 'level': 'INFO'}})\n"
            "logging.getLogger('app').info('after-dictconfig')\n"
            "provider.force_flush()\n"
        )
        with OtlpCaptureServer() as server:
            _run_python(
                script,
                env_overrides={"OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url},
            )
            assert [rec.body for rec in server.log_records] == ["after-dictconfig"]


class TestInstrumentationConfig:
    """``OTEL_PYTHON_DISABLED_INSTRUMENTATIONS`` lets services skip individual auto-instrumentors."""

    def test_parses_comma_separated_disabled_instrumentations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", " fastapi , botocore,,")
        assert disabled_instrumentations() == {"fastapi", "botocore"}


class TestSdkInternals:
    """Fail loudly when an OpenTelemetry SDK upgrade removes a private attribute this package relies on."""

    def test_batch_processor_exposes_bounded_deque_for_open_breaker_buffering(self) -> None:
        processor = BatchLogRecordProcessor(InMemoryLogRecordExporter())
        try:
            assert hasattr(processor._batch_processor._queue, "appendleft")
        finally:
            processor.shutdown()

    def test_logger_provider_exposes_atexit_handler_for_replacement(self) -> None:
        provider = LoggerProvider()
        try:
            assert callable(provider._at_exit_handler)
        finally:
            provider.shutdown()
