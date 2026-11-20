"""Tests for single-line JSON stdout fallback logging and stdout opt-out environment flags."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from opentelemetry.sdk.trace import TracerProvider

from i_dot_ai_utilities.otel._environment import (
    ENV_KEEP_STDOUT_LOGS,
    ENV_KEEP_STDOUT_LOGS_LAMBDA,
    keep_stdout_logs,
)
from i_dot_ai_utilities.otel._stdout import JsonLineFormatter, JsonStdoutHandler, install_stdout_handler

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

        monkeypatch.setenv(ENV_KEEP_STDOUT_LOGS_LAMBDA, "true")
        monkeypatch.setenv(ENV_KEEP_STDOUT_LOGS, "false")
        assert keep_stdout_logs() is False


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

    def test_includes_hex_trace_and_span_ids_only_inside_active_span(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter())
        test_logger = logging.getLogger("test.json.span")
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False
        test_logger.addHandler(handler)
        tracer = TracerProvider().get_tracer("test.json.span")
        try:
            test_logger.info("outside-span")
            with tracer.start_as_current_span("work") as span:
                expected_tid = format(span.get_span_context().trace_id, "032x")
                expected_sid = format(span.get_span_context().span_id, "016x")
                test_logger.info("inside-span")
        finally:
            test_logger.removeHandler(handler)

        outside_line, inside_line = stream.getvalue().splitlines()
        outside = json.loads(outside_line)
        inside = json.loads(inside_line)
        assert "trace_id" not in outside
        assert "span_id" not in outside
        assert inside["trace_id"] == expected_tid
        assert inside["span_id"] == expected_sid

    def test_serialises_non_json_extra_values_with_str_fallback(self) -> None:
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(JsonLineFormatter())
        test_logger = logging.getLogger("test.json.str")
        test_logger.setLevel(logging.INFO)
        test_logger.propagate = False
        test_logger.addHandler(handler)
        try:
            test_logger.info("path-check", extra={"i_ai.sample.file.path": PurePosixPath("/var/log/app")})
        finally:
            test_logger.removeHandler(handler)

        payload = json.loads(stream.getvalue().strip())
        assert payload["i_ai.sample.file.path"] == "/var/log/app"


class TestInstallStdoutHandler:
    """``install_stdout_handler`` leaves existing application console handlers untouched to avoid duplicate output."""

    def test_skips_json_stdout_handler_when_root_logger_already_has_application_handler(self) -> None:
        root = logging.getLogger()
        saved_handlers = list(root.handlers)
        app_handler = logging.StreamHandler(io.StringIO())
        try:
            root.handlers = [app_handler]
            assert install_stdout_handler() is None
            assert root.handlers == [app_handler]

            root.handlers = []
            added = install_stdout_handler()
            assert isinstance(added, JsonStdoutHandler)
            assert root.handlers == [added]
        finally:
            root.handlers = saved_handlers


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
