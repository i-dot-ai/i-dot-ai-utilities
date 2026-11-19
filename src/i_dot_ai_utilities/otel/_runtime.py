"""Execution-environment hooks driven by `configure_otel`.

Each runtime subclasses `Runtime` and overrides only the lifecycle steps that
differ from the local baseline. `configure_otel` runs the same startup sequence
against the active `Runtime` instance, so adding a runtime requires one
subclass and one entry in `_configuration._RUNTIMES`.
"""

from __future__ import annotations

import atexit
import os
from typing import TYPE_CHECKING, ClassVar, Literal

from opentelemetry.environment_variables import (
    OTEL_LOGS_EXPORTER,
    OTEL_METRICS_EXPORTER,
    OTEL_PROPAGATORS,
    OTEL_PYTHON_ID_GENERATOR,
    OTEL_TRACES_EXPORTER,
)
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.environment_variables import OTEL_EXPORTER_OTLP_PROTOCOL

from i_dot_ai_utilities.otel._environment import keep_stdout_logs, logs_export_disabled
from i_dot_ai_utilities.otel._stdout import install_stdout_handler

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
    from opentelemetry.sdk._logs.export import LogRecordExporter
    from opentelemetry.util.types import AttributeValue

RuntimeName = Literal["lambda", "ecs", "local"]


def apply_common_env_defaults(*, logs_exporter: str) -> None:
    """Set shared exporter and protocol defaults with `os.environ.setdefault` so caller variables win."""
    # Default traces and metrics exporters to "none" while otel-gateway runs a logs-only pipeline.
    os.environ.setdefault(OTEL_TRACES_EXPORTER, "none")
    os.environ.setdefault(OTEL_METRICS_EXPORTER, "none")
    os.environ.setdefault(OTEL_LOGS_EXPORTER, logs_exporter)
    os.environ.setdefault(OTEL_EXPORTER_OTLP_PROTOCOL, "http/protobuf")


def apply_aws_trace_env_defaults() -> None:
    """Default trace IDs to X-Ray format and enable X-Ray, W3C `tracecontext`, and `baggage` propagators.

    Composite propagators extract in list order, so `"xray,tracecontext,baggage"`
    lets W3C `tracecontext` overwrite `xray` when a request carries both headers.
    """
    os.environ.setdefault(OTEL_PYTHON_ID_GENERATOR, "xray")
    os.environ.setdefault(OTEL_PROPAGATORS, "xray,tracecontext,baggage")


def unregister_sdk_atexit(provider: LoggerProvider) -> None:
    """Remove the SDK's blocking `atexit` shutdown hook from `provider`.

    `LoggerProvider` registers a 30-second `atexit` hook that stalls ECS task
    shutdowns and bypasses the Lambda circuit breaker. `TestSdkInternals`
    verifies that `_at_exit_handler` still exists on SDK upgrades.
    """
    at_exit = getattr(provider, "_at_exit_handler", None)
    if at_exit is not None:
        atexit.unregister(at_exit)
        provider._at_exit_handler = None  # noqa: SLF001


class Runtime:
    """Lifecycle hooks that `configure_otel` calls in sequence for one execution environment.

    `configure_otel` calls `apply_env_defaults`, `resource_attributes`,
    `log_record_processor`, and `post_configure` once on initial setup, and
    calls `on_reconfigure` on any subsequent `configure_otel` call in the same
    process.
    """

    missing_endpoint_message: ClassVar[str]
    # Entry point names that this runtime instruments directly instead of through the generic loader.
    self_instrumented: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, *, service_name: str | None) -> None:
        self.service_name = service_name

    def apply_env_defaults(self, *, can_export: bool) -> None:
        """Populate runtime-specific `OTEL_*` environment defaults before SDK configuration."""
        apply_common_env_defaults(logs_exporter="otlp" if can_export else "none")

    def resource_attributes(self) -> dict[str, AttributeValue]:
        """Return runtime-detected OpenTelemetry resource attributes."""
        return {}

    def log_record_processor(self, exporter: LogRecordExporter) -> LogRecordProcessor:
        """Wrap `exporter` in the `LogRecordProcessor` used to export OTLP logs."""
        return BatchLogRecordProcessor(exporter)

    def post_configure(self, _provider: LoggerProvider) -> None:
        """Run runtime-specific setup after the SDK, root handler, and instrumentors are installed."""
        # Keep stdout logging active whenever OTLP export is off so disabling export never silences logs.
        if logs_export_disabled() or keep_stdout_logs():
            install_stdout_handler()

    def on_reconfigure(self) -> None:
        """Run idempotent follow-up work when `configure_otel` is called again in the same process."""


class LocalRuntime(Runtime):
    """Export logs in the background when an OTLP endpoint is set, falling back to JSON stdout locally."""

    missing_endpoint_message = (
        "OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_LOGS_ENDPOINT is not set; logs go to stdout only."
    )


__all__ = [
    "LocalRuntime",
    "Runtime",
    "RuntimeName",
    "apply_aws_trace_env_defaults",
    "apply_common_env_defaults",
    "unregister_sdk_atexit",
]
