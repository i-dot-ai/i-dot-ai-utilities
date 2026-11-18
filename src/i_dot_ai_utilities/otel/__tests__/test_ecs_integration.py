"""Subprocess integration tests for the ECS / AWS Batch container runtime with FastAPI."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import Any

import pytest

from i_dot_ai_utilities.otel.__tests__.fixtures.ecs_metadata_server import (
    CONTAINER_ARN,
    TASK_ARN,
    FakeEcsMetadataServer,
)
from i_dot_ai_utilities.otel.__tests__.fixtures.otlp_capture_server import OtlpCaptureServer
from i_dot_ai_utilities.otel._emf import EMF_EVENT_NAME, EMF_METRIC_NAME, EMF_NAMESPACE

pytest.importorskip("fastapi")
pytest.importorskip("opentelemetry.instrumentation.fastapi")

_RUNNER_MODULE = "i_dot_ai_utilities.otel.__tests__.fixtures.run_ecs_scenario"


def _container_env(extra: dict[str, str]) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OTEL_", "I_DOT_AI_OTEL_", "TEST_", "AWS_LAMBDA_", "AWS_BATCH_", "ECS_"))
    }
    env.update(extra)
    return env


def _run(extra_env: dict[str, str]) -> tuple[subprocess.CompletedProcess[str], dict[str, Any], float]:
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


class TestEcsFastApiEndToEnd:
    def test_exports_logs_with_ecs_resource_and_keeps_json_stdout(self) -> None:
        with OtlpCaptureServer() as server, FakeEcsMetadataServer() as metadata:
            proc, summary, _ = _run(
                {
                    "ECS_CONTAINER_METADATA_URI_V4": metadata.uri,
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_RESOURCE_ATTRIBUTES": "deployment.environment.name=dev",
                    "TEST_EXPLICIT_SERVICE_NAME": "sample-api",
                }
            )
            assert summary["statuses"] == [200, 200, 200]

            records = [rec for rec in server.log_records if rec.body == "Item requested"]
            assert [rec.attributes["i_ai.sample.item.id"] for rec in records] == [0, 1, 2]
            # FastAPI auto-instrumentation created a server span, so each log is trace-correlated.
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
            assert server.requests_for_path("/v1/traces") == []

            stdout_records = [rec for rec in _stdout_json(proc) if rec.get("message") == "Item requested"]
            assert len(stdout_records) == 3
            assert stdout_records[0]["level"] == "INFO"
            assert stdout_records[0]["i_ai.sample.item.id"] == 0
            assert stdout_records[0]["trace_id"] == records[0].trace_id

    def test_batch_job_attributes_and_stdout_opt_out(self) -> None:
        with OtlpCaptureServer() as server:
            proc, _, _ = _run(
                {
                    "AWS_BATCH_JOB_ID": "job-123",
                    "AWS_BATCH_JOB_ATTEMPT": "2",
                    "AWS_BATCH_JQ_NAME": "gpu-queue",
                    "OTEL_EXPORTER_OTLP_ENDPOINT": server.base_url,
                    "OTEL_SERVICE_NAME": "sample-batch",
                    "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false",
                }
            )
            resource = server.log_records[0].resource_attributes
            assert resource["i_ai.aws.batch.job.id"] == "job-123"
            assert resource["i_ai.aws.batch.job.attempt"] == "2"
            assert resource["i_ai.aws.batch.job.queue"] == "gpu-queue"
            assert proc.stdout == ""

    def test_hung_gateway_does_not_slow_requests_and_shutdown_is_capped(self) -> None:
        with OtlpCaptureServer(delay_seconds=30.0) as server:
            proc, summary, elapsed = _run(
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
        # Uncapped, the SDK would wait up to 30s for the hung export before exiting.
        assert elapsed < 10.0
        assert len([rec for rec in _stdout_json(proc) if rec.get("message") == "Item requested"]) == 20

    def test_rejecting_gateway_emits_one_rate_limited_emf_line(self) -> None:
        with OtlpCaptureServer(status_code=400) as server:
            proc, summary, _ = _run(
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

    def test_missing_endpoint_keeps_stdout_even_when_opted_out(self) -> None:
        proc, summary, _ = _run(
            {
                "AWS_BATCH_JOB_ID": "job-no-endpoint",
                "OTEL_SERVICE_NAME": "sample-api",
                "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS": "false",
            }
        )
        assert summary["statuses"] == [200, 200, 200]
        assert len([rec for rec in _stdout_json(proc) if rec.get("message") == "Item requested"]) == 3
        assert proc.stderr.count("continuing with OTLP log export disabled") == 1


class TestSigterm:
    def test_plain_script_flushes_queued_logs_on_sigterm(self) -> None:
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

    def test_flush_otel_exports_when_the_server_dies_without_atexit(self) -> None:
        # uvicorn before 0.47 sets its handler before importing the app, then restores the
        # default and re-raises SIGTERM after its graceful shutdown, so atexit never runs.
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
