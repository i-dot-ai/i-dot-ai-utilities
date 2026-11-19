"""Emit CloudWatch Embedded Metric Format failure signals when OTLP log exports fail.

CloudWatch Logs extracts metrics from stdout lines in this format on both AWS
Lambda and ECS `awslogs`, so one `ExportFailures` alarm covers both runtimes.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from typing import Final

EMF_NAMESPACE: Final[str] = "i-dot-ai/otel"
EMF_METRIC_NAME: Final[str] = "ExportFailures"
EMF_EVENT_NAME: Final[str] = "otel_log_export_failed"


def emit_export_failure(reason: str, *, dimension: str, value: str) -> None:
    """Write one stdout JSON line formatted as both a structured warning and a CloudWatch EMF metric.

    The payload excludes endpoints, headers, request bodies, tokens, and raw
    exception strings so sensitive data cannot leak to CloudWatch when an
    export fails.

    Args:
        reason: Short machine-readable failure code such as `"export_failure"`
            or `"flush_timeout"`. Never pass raw exception text.
        dimension: CloudWatch metric dimension key, such as `"FunctionName"`
            or `"ServiceName"`.
        value: Dimension value identifying the failing Lambda function or
            container service.
    """
    payload = {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [
                {
                    "Namespace": EMF_NAMESPACE,
                    "Dimensions": [[dimension]],
                    "Metrics": [{"Name": EMF_METRIC_NAME, "Unit": "Count"}],
                }
            ],
        },
        "level": "WARNING",
        "event": EMF_EVENT_NAME,
        "reason": reason,
        dimension: value,
        EMF_METRIC_NAME: 1,
    }
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


class RateLimitedFailureSignal:
    """Thread-safe callable that emits at most one EMF failure line per `interval_seconds` to prevent stdout floods."""

    def __init__(self, *, dimension: str, value: str, interval_seconds: float = 60.0) -> None:
        self._dimension = dimension
        self._value = value
        self._interval = interval_seconds
        self._last: float | None = None
        self._lock = threading.Lock()

    def __call__(self, reason: str) -> None:
        """Emit an EMF failure line for `reason`, dropping calls that arrive before `interval_seconds` elapses."""
        now = time.monotonic()
        with self._lock:
            if self._last is not None and now - self._last < self._interval:
                return
            self._last = now
        emit_export_failure(reason, dimension=self._dimension, value=self._value)


__all__ = [
    "EMF_EVENT_NAME",
    "EMF_METRIC_NAME",
    "EMF_NAMESPACE",
    "RateLimitedFailureSignal",
    "emit_export_failure",
]
