"""Tests for single-line JSON stdout fallback logging and stdout opt-out environment flags."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from typing import TYPE_CHECKING

from i_dot_ai_utilities.otel._environment import (
    ENV_KEEP_STDOUT_LOGS,
    ENV_KEEP_STDOUT_LOGS_LAMBDA,
    keep_stdout_logs,
)
from i_dot_ai_utilities.otel._stdout import JsonLineFormatter

if TYPE_CHECKING:
    import pytest


class TestStdoutEnvPrecedence:
    """Application logs mirror to stdout by default so CloudWatch retains a copy during gateway outages."""

    def test_keep_stdout_logs_defaults_on_and_prefers_current_var_over_legacy_lambda_name(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv(ENV_KEEP_STDOUT_LOGS, raising=False)
        monkeypatch.delenv(ENV_KEEP_STDOUT_LOGS_LAMBDA, raising=False)
        assert keep_stdout_logs() is True

        monkeypatch.setenv(ENV_KEEP_STDOUT_LOGS_LAMBDA, "false")
        assert keep_stdout_logs() is False

        monkeypatch.setenv(ENV_KEEP_STDOUT_LOGS, "true")
        assert keep_stdout_logs() is True


class TestJsonLineFormatter:
    """``awslogs`` splits multi-line writes into separate events, so every record must stay on one JSON line."""

    def test_formats_extras_and_multiline_traceback_onto_single_json_line(self) -> None:
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


class TestLocalStdoutFallback:
    """Local development without an OTLP endpoint warns once and writes single-line JSON logs to stdout."""

    def test_missing_endpoint_on_local_runtime_warns_and_writes_json_stdout(self) -> None:
        script = (
            "import logging\n"
            "from i_dot_ai_utilities.otel import configure_otel\n"
            "provider = configure_otel(service_name='local-dev-svc')\n"
            "logging.getLogger('local.app').info('hello-local-console', extra={'i_ai.sample.item.id': 7})\n"
            "provider.force_flush()\n"
        )
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("OTEL_", "I_DOT_AI_OTEL_", "AWS_LAMBDA_", "AWS_BATCH_", "ECS_"))
        }
        proc = subprocess.run(  # noqa: S603
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, f"stderr={proc.stderr!r}"
        (line,) = proc.stdout.splitlines()
        record = json.loads(line)
        assert record["message"] == "hello-local-console"
        assert record["i_ai.sample.item.id"] == 7
        assert "logs go to stdout only" in proc.stderr
