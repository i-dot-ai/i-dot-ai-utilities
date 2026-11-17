"""Runtime interface shared by every supported execution environment.

Each runtime (Lambda, ECS, local) subclasses :class:`Runtime` and overrides
only the steps it needs. ``configure_otel`` drives the same sequence for all
of them, so adding a runtime means adding one subclass and one entry in
``_configuration._RUNTIMES``.
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

from i_dot_ai_utilities.otel._environment import keep_stdout_logs
from i_dot_ai_utilities.otel._stdout import install_stdout_handler

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
    from opentelemetry.sdk._logs.export import LogRecordExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.util.types import AttributeValue

RuntimeName = Literal["lambda", "ecs", "local"]


def resource_attributes_from(resource: Resource) -> dict[str, AttributeValue]:
    """Return a detector's resource attributes as a plain dict for ``OpenTelemetryConfigurator``."""
    return dict(resource.attributes)


def apply_common_env_defaults(*, logs_exporter: str) -> None:
    """Set the defaults every runtime shares, without overriding explicit environment variables."""
    # shortcut: traces and metrics are off because otel-gateway only runs a logs pipeline;
    # drop these defaults once the gateway accepts traces and metrics.
    os.environ.setdefault(OTEL_TRACES_EXPORTER, "none")
    os.environ.setdefault(OTEL_METRICS_EXPORTER, "none")
    os.environ.setdefault(OTEL_LOGS_EXPORTER, logs_exporter)
    os.environ.setdefault(OTEL_EXPORTER_OTLP_PROTOCOL, "http/protobuf")


def apply_aws_trace_env_defaults() -> None:
    """Use X-Ray compatible trace IDs and accept X-Ray headers, with W3C winning on conflict."""
    os.environ.setdefault(OTEL_PYTHON_ID_GENERATOR, "xray")
    os.environ.setdefault(OTEL_PROPAGATORS, "xray,tracecontext,baggage")


def unregister_sdk_atexit(provider: LoggerProvider) -> None:
    """Remove the SDK's ``atexit`` shutdown hook from ``provider`` (SDK internal attribute)."""
    at_exit = getattr(provider, "_at_exit_handler", None)
    if at_exit is not None:
        atexit.unregister(at_exit)
        provider._at_exit_handler = None  # noqa: SLF001


class Runtime:
    """Default behaviour for one execution environment. Subclasses override single steps."""

    name: ClassVar[RuntimeName]
    missing_endpoint_message: ClassVar[str]
    # Entry point names the runtime instruments itself instead of the generic loader.
    self_instrumented: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, *, service_name: str | None) -> None:
        self.service_name = service_name

    def apply_env_defaults(self, *, endpoint_configured: bool) -> None:
        apply_common_env_defaults(logs_exporter="otlp" if endpoint_configured else "none")

    def resource_attributes(self) -> dict[str, AttributeValue]:
        return {}

    def log_record_processor(self, exporter: LogRecordExporter) -> LogRecordProcessor:
        return BatchLogRecordProcessor(exporter)

    def post_configure(self, _provider: LoggerProvider) -> None:
        if keep_stdout_logs(default=False):
            install_stdout_handler()

    def on_reconfigure(self) -> None:
        """Run when ``configure_otel`` is called again in the same process."""


class LocalRuntime(Runtime):
    """Developer machines and anything not detected as AWS."""

    name = "local"
    missing_endpoint_message = (
        "OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_LOGS_ENDPOINT is not set; using console log exporter."
    )

    def apply_env_defaults(self, *, endpoint_configured: bool) -> None:
        apply_common_env_defaults(logs_exporter="otlp" if endpoint_configured else "console")


__all__ = [
    "LocalRuntime",
    "Runtime",
    "RuntimeName",
    "apply_aws_trace_env_defaults",
    "apply_common_env_defaults",
    "resource_attributes_from",
    "unregister_sdk_atexit",
]
