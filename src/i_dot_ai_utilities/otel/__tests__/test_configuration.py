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
    InMemoryLogRecordExporter,
    SimpleLogRecordProcessor,
)

import i_dot_ai_utilities.otel as public_otel
from i_dot_ai_utilities.otel import _instrumentation
from i_dot_ai_utilities.otel.__tests__.fixtures.otlp_capture_server import OtlpCaptureServer
from i_dot_ai_utilities.otel._configuration import (
    _ExcludeInternalOtelFilter,
    _install_root_handler,
    _resolve_service_name,
    _warn_once,
    configure_otel,
    detect_runtime,
)
from i_dot_ai_utilities.otel._environment import has_otlp_logs_endpoint
from i_dot_ai_utilities.otel._instrumentation import (
    disabled_instrumentations,
    instrument_installed_libraries,
    uninstrument_installed_libraries,
)


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
        assert callable(public_otel.flush_otel)
        with pytest.raises(AttributeError, match="no_such_attr"):
            _ = public_otel.no_such_attr  # type: ignore[attr-defined]


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

    def test_importing_iai_attribute_is_lazy_and_ecs_runtime_skips_lambda_module(self) -> None:
        """Attribute helpers avoid loading runtime modules, and ``configure_otel`` loads only the active runtime."""
        lazy_script = (
            "import json, sys\n"
            "from i_dot_ai_utilities.otel import iai_attribute\n"
            "attr = iai_attribute('auth.decision.reason')\n"
            "loaded = [m for m in ('_configuration', '_ecs', '_lambda', '_instrumentation')\n"
            "          if f'i_dot_ai_utilities.otel.{m}' in sys.modules]\n"
            "print(json.dumps({'attr': attr, 'loaded': loaded}))\n"
        )
        lazy_result = json.loads(_run_python(lazy_script).stdout.strip())
        assert lazy_result == {"attr": "i_ai.auth.decision.reason", "loaded": []}

        ecs_script = (
            "import json, sys\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "configure_otel(service_name='ecs-svc', runtime='ecs')\n"
            "print(json.dumps({\n"
            "    'ecs_loaded': 'i_dot_ai_utilities.otel._ecs' in sys.modules,\n"
            "    'lambda_loaded': 'i_dot_ai_utilities.otel._lambda' in sys.modules,\n"
            "}))\n"
        )
        ecs_result = json.loads(
            _run_python(ecs_script, env_overrides={"I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false"}).stdout.strip()
        )
        assert ecs_result == {"ecs_loaded": True, "lambda_loaded": False}


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

    def test_resolves_service_name_from_otel_resource_attributes_before_argument(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OTEL_SERVICE_NAME", raising=False)
        monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "service.name=attr-svc,service.namespace=casework")
        assert _resolve_service_name("fallback-svc") == "attr-svc"

        monkeypatch.setenv("OTEL_SERVICE_NAME", "env-svc")
        assert _resolve_service_name("fallback-svc") == "env-svc"

    def test_detects_logs_specific_otlp_endpoint_without_base_endpoint(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", raising=False)
        assert has_otlp_logs_endpoint() is False

        monkeypatch.setenv("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "http://127.0.0.1:4318/v1/logs")
        assert has_otlp_logs_endpoint() is True


class TestAwsTracingDefaults:
    """AWS runtimes generate X-Ray compatible trace IDs and let W3C ``tracecontext`` override ``X-Amzn-Trace-Id``."""

    def test_aws_runtimes_generate_xray_trace_ids_and_prefer_w3c_over_xray_headers(self) -> None:
        script = (
            "import json, time\n"
            "from opentelemetry import propagate, trace\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "configure_otel(service_name='svc', runtime='ecs')\n"
            "tracer = trace.get_tracer('test')\n"
            "now = int(time.time())\n"
            "with tracer.start_as_current_span('root') as sp:\n"
            "    generated = format(sp.get_span_context().trace_id, '032x')\n"
            "xray = 'Root=1-67000001-111111111111111111111111;Parent=2222222222222222;Sampled=1'\n"
            "w3c = '00-33333333333333333333333333333333-4444444444444444-01'\n"
            "ctx_xray = propagate.extract({'X-Amzn-Trace-Id': xray})\n"
            "ctx_both = propagate.extract({'X-Amzn-Trace-Id': xray, 'traceparent': w3c})\n"
            "print(json.dumps({\n"
            "    'now': now,\n"
            "    'generated': generated,\n"
            "    'xray_only': format(trace.get_current_span(ctx_xray).get_span_context().trace_id, '032x'),\n"
            "    'both': format(trace.get_current_span(ctx_both).get_span_context().trace_id, '032x'),\n"
            "}))\n"
        )
        proc = _run_python(script, env_overrides={"I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false"})
        result = json.loads(proc.stdout.strip())
        epoch_prefix = int(result["generated"][:8], 16)
        assert abs(epoch_prefix - int(result["now"])) < 60
        assert result["xray_only"] == "67000001111111111111111111111111"
        assert result["both"] == "33333333333333333333333333333333"


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

    def test_preserves_preconfigured_root_logger_level_other_than_default_warning(self) -> None:
        root_logger = logging.getLogger()
        saved_level = root_logger.level
        provider = LoggerProvider()
        root_logger.setLevel(logging.ERROR)
        handler = _install_root_handler(provider)
        try:
            assert root_logger.level == logging.ERROR
        finally:
            root_logger.removeHandler(handler)
            root_logger.setLevel(saved_level)
            provider.shutdown()

    def test_repeat_configure_otel_returns_same_provider_without_duplicating_handlers(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _warn_once("duplicate-check", "warned once")
        _warn_once("duplicate-check", "warned once")
        assert capsys.readouterr().err.count("warned once") == 1

        script = (
            "import json, logging\n"
            "from opentelemetry.sdk._logs import LoggingHandler\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "from i_dot_ai_utilities.otel._stdout import JsonStdoutHandler\n"
            "p1 = configure_otel(service_name='svc')\n"
            "p2 = configure_otel(service_name='svc')\n"
            "handlers = logging.getLogger().handlers\n"
            "print(json.dumps({\n"
            "    'same_provider': p1 is p2,\n"
            "    'otel_handlers': sum(isinstance(h, LoggingHandler) for h in handlers),\n"
            "    'stdout_handlers': sum(isinstance(h, JsonStdoutHandler) for h in handlers),\n"
            "}))\n"
        )
        proc = _run_python(script)
        result = json.loads(proc.stdout.strip())
        assert result == {"same_provider": True, "otel_handlers": 1, "stdout_handlers": 1}


class TestAutoInstrumentation:
    """Entry-point loader respects disable flags, skips version conflicts, and fails open on broken hooks."""

    def test_parses_comma_separated_disabled_instrumentations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", " fastapi , botocore,,")
        assert disabled_instrumentations() == {"fastapi", "botocore"}

    def test_skips_disabled_wildcard_and_runtime_self_instrumented_entry_points(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        instrumented_calls: list[str] = []

        class _StubEntryPoint:
            def __init__(self, name: str) -> None:
                self.name = name
                self.dist = None

            def load(self) -> object:
                ep_name = self.name

                class _Instrumentor:
                    is_instrumented_by_opentelemetry = False

                    def instrument(self, *, skip_dep_check: bool = False) -> None:
                        assert skip_dep_check is True
                        instrumented_calls.append(ep_name)

                return _Instrumentor

        stubs = [_StubEntryPoint("fastapi"), _StubEntryPoint("aws-lambda"), _StubEntryPoint("botocore")]
        monkeypatch.setattr(_instrumentation, "entry_points", lambda **_kwargs: stubs)

        monkeypatch.setenv("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", "fastapi")
        assert instrument_installed_libraries(skip=frozenset({"aws-lambda"})) == ["botocore"]
        assert instrumented_calls == ["botocore"]

        instrumented_calls.clear()
        monkeypatch.setenv("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", "*")
        assert instrument_installed_libraries() == []
        assert instrumented_calls == []

    def test_skips_entry_point_when_distribution_has_dependency_conflict(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        conflict_dist = object()
        clean_dist = object()

        class _StubEntryPoint:
            def __init__(self, name: str, dist: object) -> None:
                self.name = name
                self.dist = dist

            def load(self) -> object:
                class _Instrumentor:
                    is_instrumented_by_opentelemetry = False

                    def instrument(self, *, skip_dep_check: bool = False) -> None:
                        pass

                return _Instrumentor

        monkeypatch.delenv("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", raising=False)
        monkeypatch.setattr(
            _instrumentation,
            "entry_points",
            lambda **_kwargs: [
                _StubEntryPoint("conflicting", conflict_dist),
                _StubEntryPoint("compatible", clean_dist),
            ],
        )
        monkeypatch.setattr(
            _instrumentation,
            "get_dist_dependency_conflicts",
            lambda dist: "version mismatch" if dist is conflict_dist else None,
        )

        assert instrument_installed_libraries() == ["compatible"]

    def test_logs_stderr_warning_and_continues_when_instrumentor_raises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        class _BrokenEntryPoint:
            name = "broken-lib"
            dist = None

            @staticmethod
            def load() -> object:
                class _BrokenInstrumentor:
                    is_instrumented_by_opentelemetry = True

                    def instrument(self, *, skip_dep_check: bool = False) -> None:
                        pass

                    def uninstrument(self) -> None:
                        msg = "uninstrument exploded"
                        raise RuntimeError(msg)

                def _factory() -> _BrokenInstrumentor:
                    msg = "broken hook"
                    raise RuntimeError(msg)

                return _factory

        class _MissingTargetEntryPoint:
            name = "optional-lib"
            dist = None

            @staticmethod
            def load() -> object:
                msg = "optional_lib"
                raise ModuleNotFoundError(msg)

        class _HealthyEntryPoint:
            name = "healthy-lib"
            dist = None

            @staticmethod
            def load() -> object:
                class _HealthyInstrumentor:
                    is_instrumented_by_opentelemetry = True

                    def uninstrument(self) -> None:
                        msg = "teardown error"
                        raise RuntimeError(msg)

                return _HealthyInstrumentor

        monkeypatch.delenv("OTEL_PYTHON_DISABLED_INSTRUMENTATIONS", raising=False)
        monkeypatch.setattr(
            _instrumentation,
            "entry_points",
            lambda **_kwargs: [_BrokenEntryPoint(), _MissingTargetEntryPoint(), _HealthyEntryPoint()],
        )

        assert instrument_installed_libraries() == ["healthy-lib"]
        err = capsys.readouterr().err
        assert "failed to instrument 'broken-lib' (RuntimeError); skipping." in err
        assert "optional-lib" not in err

        uninstrument_installed_libraries()


class TestSdkInternals:
    """Fail loudly when an OpenTelemetry SDK upgrade removes a private attribute this package relies on."""

    def test_logger_provider_exposes_atexit_handler_for_replacement(self) -> None:
        provider = LoggerProvider()
        try:
            assert callable(provider._at_exit_handler)
        finally:
            provider.shutdown()

    def test_otlp_log_exporter_exposes_closeable_transport_for_recycling_and_fork_safety(self) -> None:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter  # noqa: PLC0415

        exporter = OTLPLogExporter(endpoint="http://127.0.0.1:4318/v1/logs")
        try:
            assert callable(exporter._client._transport.close)
        finally:
            exporter.shutdown()


class TestFailSoftConfigurationAndLimits:
    """Invalid exporter or SDK limit env vars fall back to stdout, and oversized attributes are bounded by default."""

    @pytest.mark.parametrize(
        ("env_var", "bad_value"),
        [
            pytest.param("OTEL_BLRP_MAX_QUEUE_SIZE", "-5", id="negative-blrp-queue"),
            pytest.param("OTEL_ATTRIBUTE_COUNT_LIMIT", "not-an-int", id="invalid-attribute-count-limit"),
            pytest.param(
                "OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT",
                "not-an-int",
                id="invalid-logrecord-attr-length-limit",
            ),
        ],
    )
    def test_invalid_sdk_value_errors_fall_back_to_stdout_without_crashing(
        self,
        env_var: str,
        bad_value: str,
    ) -> None:
        script = (
            "import logging\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "configure_otel(service_name='svc')\n"
            "logging.getLogger('app').info('still-logging')\n"
        )
        proc = _run_python(
            script,
            env_overrides={
                "OTEL_EXPORTER_OTLP_ENDPOINT": "http://127.0.0.1:4318",
                env_var: bad_value,
            },
        )
        assert "rejected the exporter settings" in proc.stderr
        assert "http/protobuf is the only supported OTLP protocol" not in proc.stderr
        (stdout_line,) = proc.stdout.splitlines()
        assert json.loads(stdout_line)["message"] == "still-logging"

    def test_defaults_logrecord_attribute_value_length_limit_to_16384_and_respects_overrides(self) -> None:
        script = (
            "import logging\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "provider = configure_otel(service_name='svc')\n"
            "logging.getLogger('app').info('big', extra={'i_ai.test.payload': 'x' * 25000})\n"
            "provider.force_flush()\n"
        )
        with OtlpCaptureServer() as server:
            _run_python(
                script,
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false",
                },
            )
            assert len(server.log_records[0].attributes["i_ai.test.payload"]) == 16384

        with OtlpCaptureServer() as server:
            _run_python(
                script,
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT": "100",
                    "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false",
                },
            )
            assert len(server.log_records[0].attributes["i_ai.test.payload"]) == 100

        for general_limit in ("2000", "20000"):
            with OtlpCaptureServer() as server:
                _run_python(
                    script,
                    env_overrides={
                        "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                        "OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT": general_limit,
                        "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false",
                    },
                )
                assert len(server.log_records[0].attributes["i_ai.test.payload"]) == int(general_limit)
