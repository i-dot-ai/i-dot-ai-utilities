# ruff: noqa: SLF001 - these tests inspect LoggerProvider._at_exit_handler to verify SDK atexit replacement.
"""Unit and subprocess integration tests for the ECS, Fargate, EC2, and AWS Batch container runtime."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import pytest
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk.trace import TracerProvider

from i_dot_ai_utilities.otel import _ecs, flush_otel
from i_dot_ai_utilities.otel.__tests__.fixtures.ecs_metadata_server import (
    CONTAINER_ARN,
    TASK_ARN,
    FakeEcsMetadataServer,
)
from i_dot_ai_utilities.otel.__tests__.fixtures.otlp_capture_server import OtlpCaptureServer
from i_dot_ai_utilities.otel._ecs import (
    ENV_SHUTDOWN_TIMEOUT_MS,
    EcsRuntime,
    batch_resource_attributes,
    install_sigterm_handler,
    register_capped_shutdown,
)
from i_dot_ai_utilities.otel._emf import EMF_EVENT_NAME, EMF_METRIC_NAME, EMF_NAMESPACE

_RUNNER_MODULE = "i_dot_ai_utilities.otel.__tests__.fixtures.run_ecs_scenario"


def _container_env(extra: dict[str, str]) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OTEL_", "I_DOT_AI_OTEL_", "TEST_", "AWS_LAMBDA_", "AWS_BATCH_", "ECS_"))
    }
    env.update(extra)
    return env


def _run_fastapi_scenario(extra_env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], float]:
    pytest.importorskip("fastapi")
    pytest.importorskip("opentelemetry.instrumentation.fastapi")

    start = time.monotonic()
    proc = subprocess.run(  # noqa: S603
        [sys.executable, "-m", _RUNNER_MODULE],
        capture_output=True,
        text=True,
        env=_container_env(extra_env),
        check=False,
        timeout=60,
    )
    elapsed = time.monotonic() - start
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    line = next(line for line in proc.stderr.splitlines() if line.startswith("SCENARIO_JSON:"))
    return proc, json.loads(line.removeprefix("SCENARIO_JSON:")), elapsed


def _stdout_json(proc: subprocess.CompletedProcess[str]) -> list[dict[str, Any]]:
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


class TestEcsAndBatchHelpers:
    """Container helpers add AWS Batch metadata, cap shutdown below ECS stopTimeout, and catch SIGTERM."""

    def test_extracts_aws_batch_resource_attributes_only_for_non_blank_env_vars(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        for name in ("AWS_BATCH_JOB_ID", "AWS_BATCH_JOB_ATTEMPT", "AWS_BATCH_JQ_NAME"):
            monkeypatch.delenv(name, raising=False)
        assert batch_resource_attributes() == {}

        monkeypatch.setenv("AWS_BATCH_JOB_ID", "job-1")
        monkeypatch.setenv("AWS_BATCH_JQ_NAME", " ")
        assert batch_resource_attributes() == {"i_ai.aws.batch.job.id": "job-1"}

    def test_installs_sigterm_handler_only_when_current_disposition_is_sig_dfl(self) -> None:
        """Servers that install their own SIGTERM handler before ``configure_otel()`` keep ownership."""
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

    def test_installs_sigterm_handler_returns_false_off_main_thread(self) -> None:
        previous = signal.getsignal(signal.SIGTERM)
        outcomes: list[bool] = []
        try:
            signal.signal(signal.SIGTERM, signal.SIG_DFL)
            worker = threading.Thread(target=lambda: outcomes.append(install_sigterm_handler()))
            worker.start()
            worker.join(timeout=5.0)
            assert outcomes == [False]
            assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_ecs_env_defaults_set_5s_logs_timeout_unless_general_timeout_is_set(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_LOGS_TIMEOUT", raising=False)
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_TIMEOUT", "12")
        EcsRuntime(service_name="svc").apply_env_defaults(can_export=True)
        assert "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT" not in os.environ

        monkeypatch.delenv("OTEL_EXPORTER_OTLP_TIMEOUT")
        EcsRuntime(service_name="svc").apply_env_defaults(can_export=True)
        assert os.environ["OTEL_EXPORTER_OTLP_LOGS_TIMEOUT"] == "5"

    def test_replaces_unbounded_sdk_atexit_hook_with_capped_shutdown(self) -> None:
        """The SDK's ``atexit`` hook waits up to 30 s, which exhausts the default 30 s ECS ``stopTimeout``."""
        provider = LoggerProvider()
        assert provider._at_exit_handler is not None

        tracer_provider = TracerProvider()
        assert tracer_provider._atexit_handler is not None

        register_capped_shutdown(provider, 0.1, tracer_provider=tracer_provider)
        assert provider._at_exit_handler is None
        assert tracer_provider._atexit_handler is None
        provider.shutdown()
        tracer_provider.shutdown()

    def test_flush_otel_caps_wait_at_shutdown_timeout_and_returns_false_before_configure(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        release = threading.Event()

        class _HungProvider:
            def force_flush(self, _timeout_millis: int) -> bool:
                return release.wait(10)

        monkeypatch.setenv(ENV_SHUTDOWN_TIMEOUT_MS, "100")
        monkeypatch.setattr(_ecs, "get_logger_provider", _HungProvider)
        start = time.monotonic()
        try:
            assert flush_otel() is False
            assert time.monotonic() - start < 2.0
        finally:
            release.set()

        monkeypatch.setattr(_ecs, "get_logger_provider", object)
        assert flush_otel() is False

        class _FastProvider:
            @staticmethod
            def force_flush(_timeout_millis: int) -> bool:
                return True

        monkeypatch.setattr(_ecs, "get_logger_provider", _FastProvider)
        assert flush_otel() is True


class TestEcsFastApiEndToEnd:
    """FastAPI on ECS exports logs in the background without blocking requests or hanging task shutdown."""

    def test_exports_trace_correlated_request_logs_with_ecs_metadata_and_json_stdout_copy(self) -> None:
        with OtlpCaptureServer() as server, FakeEcsMetadataServer() as metadata:
            proc, summary, _ = _run_fastapi_scenario(
                {
                    "ECS_CONTAINER_METADATA_URI_V4": metadata.uri,
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=dev",
                    "ENVIRONMENT": "ignored-because-the-attribute-is-set",
                    "TEST_EXPLICIT_SERVICE_NAME": "sample-api",
                }
            )
            assert summary["statuses"] == [200, 200, 200]

            records = [rec for rec in server.log_records if rec.body == "Item requested"]
            assert [rec.attributes["i_ai.sample.item.id"] for rec in records] == [0, 1, 2]
            assert all(len(rec.trace_id) == 32 for rec in records)
            assert len({rec.trace_id for rec in records}) == 3

            resource = records[0].resource_attributes
            assert resource["service.name"] == "sample-api"
            assert resource["deployment.environment.name"] == "dev"
            assert resource["cloud.platform"] == "aws_ecs"
            assert resource["aws.ecs.task.arn"] == TASK_ARN
            assert resource["aws.ecs.container.arn"] == CONTAINER_ARN
            assert resource["aws.ecs.launchtype"] == "fargate"
            assert resource["aws.ecs.task.family"] == "test-service"
            assert len(server.requests_for_path("/v1/traces")) >= 1
            assert {span.trace_id for span in server.spans} >= {rec.trace_id for rec in records}
            assert server.spans[0].resource_attributes["service.name"] == "sample-api"

            stdout_records = [rec for rec in _stdout_json(proc) if rec.get("message") == "Item requested"]
            assert len(stdout_records) == 3
            assert stdout_records[0]["level"] == "INFO"
            assert stdout_records[0]["i_ai.sample.item.id"] == 0
            assert stdout_records[0]["trace_id"] == records[0].trace_id

    def test_attaches_aws_batch_resource_attributes_and_honours_stdout_opt_out(self) -> None:
        with OtlpCaptureServer() as server:
            proc, _, _ = _run_fastapi_scenario(
                {
                    "AWS_BATCH_JOB_ID": "job-123",
                    "AWS_BATCH_JOB_ATTEMPT": "2",
                    "AWS_BATCH_JQ_NAME": "gpu-queue",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "sample-batch",
                    "ENVIRONMENT": "preprod",
                    "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false",
                }
            )
            resource = server.log_records[0].resource_attributes
            assert resource["deployment.environment.name"] == "preprod"
            assert resource["i_ai.aws.batch.job.id"] == "job-123"
            assert resource["i_ai.aws.batch.job.attempt"] == "2"
            assert resource["i_ai.aws.batch.job.queue"] == "gpu-queue"
            assert proc.stdout == ""

    def test_hung_gateway_never_slows_requests_and_caps_process_exit_time(self) -> None:
        """Uncapped, ``BatchLogRecordProcessor.shutdown()`` waits up to 30 s when the gateway stalls."""
        with OtlpCaptureServer(delay_seconds=30.0) as server:
            proc, summary, elapsed = _run_fastapi_scenario(
                {
                    "AWS_BATCH_JOB_ID": "job-hung",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "sample-api",
                    "OTEL_EXPORTER_OTLP_LOGS_TIMEOUT": "20",
                    "OTEL_BLRP_SCHEDULE_DELAY": "50",
                    "I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS": "1000",
                    "TEST_REQUEST_COUNT": "20",
                    "TEST_WAIT_FOR_EXPORT_SECONDS": "0.3",
                }
            )
        assert summary["statuses"] == [200] * 20
        assert max(summary["latencies_ms"]) < 500.0
        assert elapsed < 10.0
        assert len([rec for rec in _stdout_json(proc) if rec.get("message") == "Item requested"]) == 20

    def test_rejecting_gateway_emits_single_rate_limited_emf_warning(self) -> None:
        with OtlpCaptureServer(status_code=400) as server:
            proc, summary, _ = _run_fastapi_scenario(
                {
                    "AWS_BATCH_JOB_ID": "job-reject",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "sample-api",
                    "OTEL_BLRP_SCHEDULE_DELAY": "50",
                    "OTEL_BLRP_MAX_EXPORT_BATCH_SIZE": "1",
                    "TEST_WAIT_FOR_EXPORT_SECONDS": "0.5",
                }
            )
            assert len(server.requests_for_path("/v1/logs")) > 1
        assert summary["statuses"] == [200, 200, 200]
        emf_lines = [rec for rec in _stdout_json(proc) if rec.get("event") == EMF_EVENT_NAME]
        assert len(emf_lines) == 1
        emf = emf_lines[0]
        assert emf["reason"] == "export_failure"
        assert emf["ServiceName"] == "sample-api"
        assert emf[EMF_METRIC_NAME] == 1
        assert emf["_aws"]["CloudWatchMetrics"][0]["Namespace"] == EMF_NAMESPACE
        assert emf["_aws"]["CloudWatchMetrics"][0]["Dimensions"] == [["ServiceName"]]

    def test_missing_endpoint_forces_json_stdout_even_when_opted_out(self) -> None:
        proc, summary, _ = _run_fastapi_scenario(
            {
                "AWS_BATCH_JOB_ID": "job-no-endpoint",
                "OTEL_SERVICE_NAME": "sample-api",
                "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false",
            }
        )
        assert summary["statuses"] == [200, 200, 200]
        assert len([rec for rec in _stdout_json(proc) if rec.get("message") == "Item requested"]) == 3
        assert proc.stderr.count("continuing with OTLP log export disabled") == 1

    def test_unsupported_otlp_protocol_warns_and_falls_back_to_json_stdout(self) -> None:
        """``http/protobuf`` is the only supported OTLP protocol; ``http/json`` and ``grpc`` fail soft at start-up."""
        with OtlpCaptureServer() as server:
            proc, summary, _ = _run_fastapi_scenario(
                {
                    "AWS_BATCH_JOB_ID": "job-bad-protocol",
                    "OTEL_SERVICE_NAME": "sample-api",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_EXPORTER_OTLP_LOGS_PROTOCOL": "http/json",
                    "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false",
                }
            )
            assert summary["statuses"] == [200, 200, 200]
            assert len([rec for rec in _stdout_json(proc) if rec.get("message") == "Item requested"]) == 3
            assert proc.stderr.count("rejected the exporter settings") == 1
            assert server.requests == []


class TestEcsShutdownSignals:
    """Stopping an ECS or Batch task sends SIGTERM; queued logs flush via atexit or an explicit lifespan flush."""

    def test_plain_script_flushes_queued_logs_on_sigterm_via_atexit(self) -> None:
        """Python's default SIGTERM kills the process without running ``atexit``; ``EcsRuntime`` exits cleanly."""
        script = (
            "import logging, sys, time\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "configure_otel(service_name='sample-worker')\n"
            "logging.getLogger('worker').info('before-sigterm')\n"
            "print('READY', flush=True, file=sys.stderr)\n"
            "time.sleep(30)\n"
        )
        with OtlpCaptureServer() as server:
            proc = subprocess.Popen(  # noqa: S603
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_container_env(
                    {
                        "AWS_BATCH_JOB_ID": "job-sigterm",
                        "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                        "OTEL_BLRP_SCHEDULE_DELAY": "60000",
                    }
                ),
            )
            assert proc.stderr is not None
            assert "READY" in proc.stderr.readline()
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=15)
            assert proc.returncode == 128 + signal.SIGTERM
            assert [rec.body for rec in server.log_records] == ["before-sigterm"]

    def test_explicit_flush_otel_exports_logs_when_server_exits_without_atexit(self) -> None:
        """uvicorn before 0.47 restores ``SIG_DFL`` and re-raises SIGTERM after lifespan shutdown, skipping atexit."""
        script = (
            "import logging, signal, sys, time\n"
            "from i_dot_ai_utilities.otel import configure_otel, flush_otel\n"
            "stop = []\n"
            "signal.signal(signal.SIGTERM, lambda signum, frame: stop.append(signum))\n"
            "configure_otel(service_name='sample-api')\n"
            "logging.getLogger('api').info('before-sigterm')\n"
            "print('READY', flush=True, file=sys.stderr)\n"
            "while not stop:\n"
            "    time.sleep(0.05)\n"
            "print(f'FLUSHED={flush_otel()}', flush=True, file=sys.stderr)\n"
            "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
            "signal.raise_signal(signal.SIGTERM)\n"
        )
        with OtlpCaptureServer() as server:
            proc = subprocess.Popen(  # noqa: S603
                [sys.executable, "-c", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=_container_env(
                    {
                        "AWS_BATCH_JOB_ID": "job-old-uvicorn",
                        "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                        "OTEL_BLRP_SCHEDULE_DELAY": "60000",
                    }
                ),
            )
            assert proc.stderr is not None
            assert "READY" in proc.stderr.readline()
            proc.send_signal(signal.SIGTERM)
            _, stderr = proc.communicate(timeout=15)
            assert proc.returncode == -signal.SIGTERM
            assert "FLUSHED=True" in stderr
            assert [rec.body for rec in server.log_records] == ["before-sigterm"]
