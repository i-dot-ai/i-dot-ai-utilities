"""Environment variable helpers shared by every runtime."""

from __future__ import annotations

import os
from typing import Final

from opentelemetry.environment_variables import OTEL_LOGS_EXPORTER

ENV_KEEP_STDOUT_LOGS: Final[str] = "I_DOT_AI_OTEL_KEEP_STDOUT_LOGS"
# Older name from the first Lambda release; still honoured everywhere.
ENV_KEEP_STDOUT_LOGS_LAMBDA: Final[str] = "I_DOT_AI_OTEL_LAMBDA_KEEP_STDOUT_LOGS"

_TRUTHY: Final[frozenset[str]] = frozenset({"1", "true", "yes"})


def has_otlp_logs_endpoint() -> bool:
    """Return True when a base or logs-specific OTLP endpoint is configured."""
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "").strip()
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )


def logs_export_disabled() -> bool:
    """Return True when ``OTEL_LOGS_EXPORTER`` is unset or ``none``, so no log leaves the process via OTel."""
    return os.environ.get(OTEL_LOGS_EXPORTER, "").strip().lower() in {"", "none"}


def env_flag(name: str, *, default: bool) -> bool:
    """Return a boolean environment flag, or ``default`` when it is unset or blank."""
    raw = os.environ.get(name, "").strip().lower()
    return raw in _TRUTHY if raw else default


def keep_stdout_logs(*, default: bool) -> bool:
    """Return whether application logs should also go to stdout alongside OTLP."""
    for name in (ENV_KEEP_STDOUT_LOGS, ENV_KEEP_STDOUT_LOGS_LAMBDA):
        if os.environ.get(name, "").strip():
            return env_flag(name, default=default)
    return default


def env_number(name: str, default: float) -> float:
    """Return a non-negative number from the environment, or ``default`` if unset or invalid."""
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
    "has_otlp_logs_endpoint",
    "keep_stdout_logs",
    "logs_export_disabled",
]
