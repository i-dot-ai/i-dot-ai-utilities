"""Tests for OTLP log emitter processor."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock

_PROCESSOR_PATH = (
    Path(__file__).resolve().parents[2] / "_otel" / "otlp_log_processor.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "otlp_log_processor_under_test", _PROCESSOR_PATH
)
_MOD = importlib.util.module_from_spec(_SPEC)
assert _SPEC.loader is not None
_SPEC.loader.exec_module(_MOD)
otel_otlp_log_emitter_processor = _MOD.otel_otlp_log_emitter_processor


class OtlpLogEmitterProcessorTests(unittest.TestCase):
    def test_emitter_returns_event_dict_unchanged_on_proxy_logger(self) -> None:
        event = {"event": "hello", "level": "info", "user_id": 1}
        out = otel_otlp_log_emitter_processor(None, "info", event)
        self.assertEqual(out["event"], "hello")
        self.assertEqual(out["user_id"], 1)

    def test_processor_emits_with_nonzero_timestamp(self) -> None:
        event = {
            "event": "hello",
            "level": "info",
            "timestamp": "2026-07-29T12:00:00Z",
            "poc_run_id": "poc-ts-001",
        }
        mock_logger = MagicMock()
        mock_severity = types.SimpleNamespace(
            DEBUG=5,
            INFO=9,
            WARN=13,
            ERROR=17,
            FATAL=21,
        )

        otel_logs = types.ModuleType("opentelemetry._logs")
        otel_logs.SeverityNumber = mock_severity
        otel_logs.get_logger = MagicMock(return_value=mock_logger)

        otel_context = types.ModuleType("opentelemetry.context")
        otel_context.get_current = MagicMock(return_value=None)

        otel_root = types.ModuleType("opentelemetry")
        otel_root.context = otel_context

        sys.modules["opentelemetry"] = otel_root
        sys.modules["opentelemetry.context"] = otel_context
        sys.modules["opentelemetry._logs"] = otel_logs
        try:
            out = otel_otlp_log_emitter_processor(None, "info", event)
        finally:
            for name in (
                "opentelemetry._logs",
                "opentelemetry.context",
                "opentelemetry",
            ):
                sys.modules.pop(name, None)

        self.assertIs(out, event)
        mock_logger.emit.assert_called_once()
        kwargs = mock_logger.emit.call_args.kwargs
        self.assertEqual(kwargs["body"], "hello")
        self.assertEqual(kwargs["severity_text"], "INFO")
        self.assertEqual(kwargs["attributes"], {"poc_run_id": "poc-ts-001"})
        self.assertNotIn("timestamp", kwargs["attributes"])
        self.assertIsInstance(kwargs["timestamp"], int)
        self.assertGreater(kwargs["timestamp"], 0)


if __name__ == "__main__":
    unittest.main()
