"""Parse `OTEL_*` and `I_DOT_AI_OTEL_*` environment variables with fallback defaults."""

from __future__ import annotations

import os
from typing import Final

from opentelemetry.environment_variables import OTEL_LOGS_EXPORTER

ENV_KEEP_STDOUT_LOGS: Final[str] = "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS"
# Legacy variable name from the initial Lambda release. ENV_KEEP_STDOUT_LOGS takes precedence when both are set.
ENV_KEEP_STDOUT_LOGS_LAMBDA: Final[str] = "I_DOT_AI_OTEL_LAMBDA_KEEP_STDOUT_LOGS"

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes"})


def is_container_runtime() -> bool:
    """Return `True` when `ECS_CONTAINER_METADATA_URI_V4` or `AWS_BATCH_JOB_ID` is set."""
    return bool(
        os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "").strip() or os.environ.get("AWS_BATCH_JOB_ID", "").strip()
    )


def has_otlp_endpoint() -> bool:
    """Return `True` when any OTLP exporter endpoint variable is non-empty."""
    return any(
        os.environ.get(name, "").strip()
        for name in (
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT",
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT",
        )
    )


def logs_export_disabled() -> bool:
    """Return `True` when `OTEL_LOGS_EXPORTER` is unset, whitespace-only, or `"none"`."""
    return os.environ.get(OTEL_LOGS_EXPORTER, "").strip().lower() in {"", "none"}


def env_flag(name: str, *, default: bool) -> bool:
    """Parse `name` as a boolean flag, returning `default` when unset or whitespace-only.

    Case-insensitive `"1"`, `"true"`, and `"yes"` return `True`. Any other
    non-empty string returns `False`.
    """
    raw = os.environ.get(name, "").strip().lower()
    return raw in _TRUTHY if raw else default


def keep_stdout_logs() -> bool:
    """Return whether application logs should also write to stdout alongside OTLP export.

    Checks `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS` before the legacy
    `I_DOT_AI_OTEL_LAMBDA_KEEP_STDOUT_LOGS` variable, and defaults to `True`
    when neither is set.
    """
    for name in (ENV_KEEP_STDOUT_LOGS, ENV_KEEP_STDOUT_LOGS_LAMBDA):
        if os.environ.get(name, "").strip():
            return env_flag(name, default=True)
    return True


def env_number(name: str, default: float) -> float:
    """Parse `name` as a non-negative `float`, returning `default` when unset, unparseable, or negative."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value >= 0.0 else default


__all__ = [
    "ENV_KEEP_STDOUT_LOGS",
    "ENV_KEEP_STDOUT_LOGS_LAMBDA",
    "env_flag",
    "env_number",
    "has_otlp_endpoint",
    "is_container_runtime",
    "keep_stdout_logs",
    "logs_export_disabled",
]
