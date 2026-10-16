"""Subprocess integration and import-boundary tests for native OpenTelemetry support."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from i_dot_ai_utilities.otel.__tests__.fixtures.otlp_capture_server import (
    OtlpCaptureServer,
)
from i_dot_ai_utilities.otel._emf import EMF_EVENT_NAME, EMF_METRIC_NAME, EMF_NAMESPACE

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


class TestLambdaEndToEnd:
    def test_logs_only_default_sends_no_traces_or_metrics_and_suppresses_stdout(self) -> None:
        with OtlpCaptureServer() as server:
            proc, summary = _run_scenario(
                [
                    {
                        "request_id": "req-happy-1",
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
                    "OTEL_EXPORTER_OTLP_HEADERS": "x-custom-test=iai-test",
                    "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=dev",
                    "TEST_EXPLICIT_SERVICE_NAME": "core-auth-api",
                    "TEST_CALL_CONFIGURE_TWICE": "1",
                    "TEST_INSTALL_USER_HANDLER": "1",
                },
            )

            assert proc.stdout == ""
            assert summary["runtime_handler_output"] == ""
            assert "Authorisation request started for casework" in summary["user_handler_output"]
            assert summary["structlog_imported"] is False

            assert len(server.requests_for_path("/v1/logs")) == 1
            assert server.requests_for_path("/v1/traces") == []
            assert server.requests_for_path("/v1/metrics") == []

            req = server.requests_for_path("/v1/logs")[0]
            assert req.headers.get("x-custom-test") == "iai-test"
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

            # Resource attributes from AwsLambdaResourceDetector + OTEL_RESOURCE_ATTRIBUTES + service_name
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

            # Standard code.* attributes from SDK LoggingHandler; no sensitive event fields
            attrs = record.attributes
            assert attrs["application"] == "casework"
            assert "code.file.path" in attrs
            assert attrs["code.function.name"] == "lambda_handler"
            assert isinstance(attrs["code.line.number"], int)
            serialized_attrs = json.dumps(attrs)
            assert "secret-jwt-token" not in serialized_attrs
            assert "eyJhbGciOiJSUzI1NiJ9" not in serialized_attrs
            assert "authorization" not in serialized_attrs.lower()

    def test_flushes_on_caught_error_and_on_raised_exception_preserving_traceback(self) -> None:
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

    def test_one_invocation_with_multiple_logs_produces_single_post(self) -> None:
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

    def test_low_remaining_time_skips_flush_and_next_invocation_drains_queue(self) -> None:
        with OtlpCaptureServer() as server:
            proc, _ = _run_scenario(
                [
                    {
                        "request_id": "req-tight",
                        "remaining_ms": 1500,  # below 2000ms timeout + 250ms margin
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
            apps = [rec.attributes.get("application") for rec in server.log_records]
            assert apps == ["first-invoke", "second-invoke"]

    def test_unreachable_endpoint_respects_timeout_opens_breaker_and_emits_emf_once(self) -> None:
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
            # First invocation hits the 0.5s timeout budget; second skips flush immediately via breaker.
            assert first["elapsed_ms"] < 1500.0
            assert second["elapsed_ms"] < 200.0
            assert len(server.requests_for_path("/v1/logs")) == 1

            stdout_lines = [line for line in proc.stdout.splitlines() if line.strip()]
            assert len(stdout_lines) == 1
            emf = json.loads(stdout_lines[0])
            assert emf["level"] == "WARNING"
            assert emf["event"] == EMF_EVENT_NAME
            assert emf["FunctionName"] == "core-auth-api-authorise-token"
            assert emf[EMF_METRIC_NAME] == 1
            assert emf["_aws"]["CloudWatchMetrics"][0]["Namespace"] == EMF_NAMESPACE

    def test_service_name_precedence_and_missing_warning(self) -> None:
        with OtlpCaptureServer() as server:
            _, _ = _run_scenario(
                [{"request_id": "req-prec", "remaining_ms": 4500, "event": {"mode": "ok"}}],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "env-service-name",
                    "TEST_EXPLICIT_SERVICE_NAME": "explicit-service-name",
                },
            )
            assert server.log_records[0].resource_attributes["service.name"] == "explicit-service-name"

        with OtlpCaptureServer() as server:
            _, _ = _run_scenario(
                [{"request_id": "req-env", "remaining_ms": 4500, "event": {"mode": "ok"}}],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "env-service-name",
                },
            )
            assert server.log_records[0].resource_attributes["service.name"] == "env-service-name"

        with OtlpCaptureServer() as server:
            proc, _ = _run_scenario(
                [{"request_id": "req-missing", "remaining_ms": 4500, "event": {"mode": "ok"}}],
                env_overrides={
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "TEST_CALL_CONFIGURE_TWICE": "1",
                },
            )
            assert proc.stderr.count("service_name is not set") == 1

    def test_missing_endpoint_on_lambda_warns_once_and_emits_no_stdout_logs(self) -> None:
        proc, summary = _run_scenario(
            [{"request_id": "req-no-endpoint", "remaining_ms": 4500, "event": {"mode": "ok"}}],
            env_overrides={
                "OTEL_SERVICE_NAME": "core-auth-api",
                "TEST_CALL_CONFIGURE_TWICE": "1",
            },
        )
        assert proc.stdout == ""
        assert summary["runtime_handler_output"] == ""
        assert proc.stderr.count("continuing with OTLP log export disabled") == 1

    def test_missing_endpoint_locally_uses_console_log_exporter(self) -> None:
        script = (
            "import logging\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "provider = configure_otel(service_name='local-dev-svc')\n"
            "logging.getLogger('local.app').info('hello-local-console')\n"
            "provider.force_flush()\n"
        )
        env = {k: v for k, v in os.environ.items() if not k.startswith("OTEL_") and not k.startswith("AWS_LAMBDA_")}
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, f"stderr={proc.stderr!r}"
        assert "hello-local-console" in proc.stdout
        assert "using console log exporter" in proc.stderr


class TestImportBoundaries:
    def test_base_package_imports_without_opentelemetry(self) -> None:
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
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc.returncode == 0, f"stderr={proc.stderr!r}"
        assert "OTEL_IMPORT_BLOCKED_OK" in proc.stdout

    def test_local_configure_otel_works_without_lambda_instrumentors_and_lambda_requires_them(self) -> None:
        block_lambda_inst = (
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
        local_script = (
            block_lambda_inst
            + "from i_dot_ai_utilities.otel import configure_otel\n"
            + "configure_otel(service_name='container-svc', runtime='local')\n"
            + "print('LOCAL_OK')\n"
        )
        proc_local = subprocess.run(  # noqa: S603
            [sys.executable, "-c", local_script],
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if not k.startswith("AWS_LAMBDA_")},
            check=False,
        )
        assert proc_local.returncode == 0, f"stderr={proc_local.stderr!r}"
        assert "LOCAL_OK" in proc_local.stdout

        lambda_script = (
            block_lambda_inst
            + "from i_dot_ai_utilities.otel import configure_otel\n"
            + "try:\n"
            + "    configure_otel(service_name='lambda-svc', runtime='lambda')\n"
            + "except ImportError:\n"
            + "    print('LAMBDA_IMPORT_ERROR_OK')\n"
        )
        proc_lambda = subprocess.run(  # noqa: S603
            [sys.executable, "-c", lambda_script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert proc_lambda.returncode == 0, f"stderr={proc_lambda.stderr!r}"
        assert "LAMBDA_IMPORT_ERROR_OK" in proc_lambda.stdout
