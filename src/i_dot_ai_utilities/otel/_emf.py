"""CloudWatch Embedded Metric Format (EMF) failure signal for Lambda OTLP exports."""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Final

EMF_NAMESPACE: Final[str] = "i-dot-ai/otel"
EMF_METRIC_NAME: Final[str] = "ExportFailures"
EMF_EVENT_NAME: Final[str] = "otel_log_export_failed"


def emit_lambda_export_failure(reason: str) -> None:
    """Write one stdout line that is both a structured warning and an EMF metric.

    The payload intentionally excludes endpoints, headers, request bodies,
    tokens, and raw exception strings so sensitive data can never leak to
    CloudWatch when an export fails.
    """
    function_name = os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or "unknown"
    timestamp_ms = int(time.time() * 1000)
    payload = {
        "_aws": {
            "Timestamp": timestamp_ms,
            "CloudWatchMetrics": [
                {
                    "Namespace": EMF_NAMESPACE,
                    "Dimensions": [["FunctionName"]],
                    "Metrics": [{"Name": EMF_METRIC_NAME, "Unit": "Count"}],
                }
            ],
        },
        "level": "WARNING",
        "event": EMF_EVENT_NAME,
        "reason": reason,
        "FunctionName": function_name,
        EMF_METRIC_NAME: 1,
    }
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


__all__ = [
    "EMF_EVENT_NAME",
    "EMF_METRIC_NAME",
    "EMF_NAMESPACE",
    "emit_lambda_export_failure",
]
