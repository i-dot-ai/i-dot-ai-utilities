"""CloudWatch Embedded Metric Format (EMF) failure signal for OTLP log exports.

CloudWatch Logs turns any log event in this format into a metric, whether it
arrives from Lambda or from ECS ``awslogs``, so one alarm pattern covers both.
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
    """Write one stdout line that is both a structured warning and an EMF metric.

    The payload intentionally excludes endpoints, headers, request bodies,
    tokens, and raw exception strings so sensitive data can never leak to
    CloudWatch when an export fails.
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
    """Emit at most one EMF failure line per ``interval_seconds``, so an outage cannot flood stdout."""

    def __init__(self, *, dimension: str, value: str, interval_seconds: float = 60.0) -> None:
        self._dimension = dimension
        self._value = value
        self._interval = interval_seconds
        self._last: float | None = None
        self._lock = threading.Lock()

    def __call__(self, reason: str) -> None:
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
