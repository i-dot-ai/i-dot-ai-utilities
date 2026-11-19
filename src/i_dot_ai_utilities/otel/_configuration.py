"""Process-wide OpenTelemetry bootstrap and runtime detection.

`configure_otel` runs the same six-step sequence on every runtime, while
`Runtime` subclasses customise individual hooks:

1. Set environment defaults with `os.environ.setdefault` so explicit caller variables win.
2. Build resource attributes from the runtime detector, resolved service name, and deployment environment.
3. Configure SDK providers via `OpenTelemetryConfigurator`, running `AttributeNamespaceProcessor` before export.
4. Attach the OpenTelemetry `LoggingHandler` to Python's root logger.
5. Instrument every installed library registered under `opentelemetry_instrumentor`.
6. Run the runtime's `post_configure` hook for handler wrapping, shutdown caps, and stdout fallback.
"""

from __future__ import annotations

import logging
import os
import sys
import warnings
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from opentelemetry import propagate
from opentelemetry._logs import get_logger_provider
from opentelemetry.distro import OpenTelemetryConfigurator
from opentelemetry.environment_variables import OTEL_LOGS_EXPORTER, OTEL_METRICS_EXPORTER, OTEL_TRACES_EXPORTER
from opentelemetry.sdk._configuration import _overwrite_logging_config_fns
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk.resources import OTELResourceDetector

from i_dot_ai_utilities.otel._attributes import AttributeNamespaceProcessor
from i_dot_ai_utilities.otel._ecs import EcsRuntime, is_container_runtime
from i_dot_ai_utilities.otel._environment import has_otlp_logs_endpoint
from i_dot_ai_utilities.otel._instrumentation import instrument_installed_libraries
from i_dot_ai_utilities.otel._lambda import LambdaRuntime
from i_dot_ai_utilities.otel._runtime import LocalRuntime, Runtime, RuntimeName

if TYPE_CHECKING:
    from opentelemetry.util.types import AttributeValue

_RUNTIMES: dict[RuntimeName, type[Runtime]] = {
    "lambda": LambdaRuntime,
    "ecs": EcsRuntime,
    "local": LocalRuntime,
}

_INTERNAL_OTEL_LOGGER_PREFIX = "opentelemetry"
_NOISY_LIBRARY_LOGGERS = ("botocore", "boto3", "urllib3")
_ENVIRONMENT_ATTRIBUTE = "deployment.environment.name"


@dataclass
class _State:
    """Process-wide objects created once by `configure_otel`."""

    runtime: Runtime | None = None
    logger_provider: LoggerProvider | None = None
    root_logging_handler: LoggingHandler | None = None
    warned: set[str] = field(default_factory=set)


_state = _State()


class _ExcludeInternalOtelFilter(logging.Filter):
    """Drop `opentelemetry.*` log records so exporter errors cannot log back into the exporter."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        return not (name == _INTERNAL_OTEL_LOGGER_PREFIX or name.startswith(f"{_INTERNAL_OTEL_LOGGER_PREFIX}."))


def detect_runtime(runtime: RuntimeName | None = None) -> RuntimeName:
    """Select the active runtime from an explicit override or AWS environment variables.

    Checks `AWS_LAMBDA_FUNCTION_NAME` first, then `ECS_CONTAINER_METADATA_URI_V4`
    and `AWS_BATCH_JOB_ID`, and falls back to `"local"`. ECS tasks on Fargate or
    EC2 and AWS Batch jobs share the `"ecs"` runtime because both run as
    long-lived containers with background OTLP export and `SIGTERM` shutdown.

    Args:
        runtime: Explicit runtime override. When `None`, inspects AWS environment
            variables.

    Returns:
        `"lambda"`, `"ecs"`, or `"local"`.

    Raises:
        ValueError: If `runtime` is not a supported runtime name.
    """
    if runtime is not None:
        if runtime not in _RUNTIMES:
            msg = f"Unsupported runtime {runtime!r}; expected one of {sorted(_RUNTIMES)}."
            raise ValueError(msg)
        return runtime
    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip():
        return "lambda"
    if is_container_runtime():
        return "ecs"
    return "local"


def _warn_once(key: str, message: str) -> None:
    """Write `message` to `stderr` at most once per process for each `key`."""
    if key in _state.warned:
        return
    _state.warned.add(key)
    sys.stderr.write(f"i_dot_ai_utilities.otel: {message}\n")


def _env_resource_attributes() -> dict[str, AttributeValue]:
    """Parse `OTEL_SERVICE_NAME` and `OTEL_RESOURCE_ATTRIBUTES` via `OTELResourceDetector` to match SDK precedence."""
    return dict(OTELResourceDetector().detect().attributes)


def _resolve_service_name(service_name: str | None) -> str | None:
    """Resolve `service.name` from the environment first, falling back to `service_name`.

    Shared Terraform modules set `OTEL_SERVICE_NAME` to an allow-listed name.
    That value wins over `service_name` because `otel-gateway` drops
    unrecognised `service.name` values while still returning HTTP 200.
    """
    from_env = str(_env_resource_attributes().get("service.name", "")).strip()
    return from_env or (service_name or "").strip() or None


def _build_sdk_logging_handler(provider: LoggerProvider) -> LoggingHandler:
    """Create a root-level SDK `LoggingHandler` that excludes internal OpenTelemetry logs."""
    # Suppress the SDK's deprecation warning until opentelemetry-instrumentation-logging ships a stable replacement.
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="`LoggingHandler` in `opentelemetry-sdk` is deprecated.*",
            category=DeprecationWarning,
        )
        handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    handler.addFilter(_ExcludeInternalOtelFilter())
    return handler


def _install_root_handler(provider: LoggerProvider) -> LoggingHandler:
    """Attach the OpenTelemetry handler to Python's root logger and apply default log levels."""
    handler = _build_sdk_logging_handler(provider)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    # Re-attach the handler if the app or Alembic later calls dictConfig, fileConfig, or basicConfig,
    # which would otherwise clear root handlers and stop OTLP export without raising an error.
    _overwrite_logging_config_fns(handler)
    if root_logger.level == logging.WARNING:
        # Python's root logger defaults to WARNING. Lower it to INFO so standard logger.info() calls
        # export without extra setup. Callers that want WARNING must set it after configure_otel().
        root_logger.setLevel(logging.INFO)
    for name in _NOISY_LIBRARY_LOGGERS:
        # Default AWS SDK and HTTP client loggers to WARNING so per-request INFO logs do not export
        # unless the application configured a level explicitly.
        library_logger = logging.getLogger(name)
        if library_logger.level == logging.NOTSET:
            library_logger.setLevel(logging.WARNING)
    return handler


def _resource_attributes(runtime: Runtime) -> dict[str, AttributeValue]:
    """Build SDK resource attributes from the runtime detector, resolved service name, and `ENVIRONMENT`."""
    attributes = runtime.resource_attributes()
    # Resource.create() merges this dict on top of OTEL_RESOURCE_ATTRIBUTES and OTEL_SERVICE_NAME,
    # and runtime.service_name already gives those environment variables precedence.
    if runtime.service_name:
        attributes["service.name"] = runtime.service_name
    # Populate deployment.environment.name from ENVIRONMENT for Loki label indexing when
    # OTEL_RESOURCE_ATTRIBUTES does not already define it.
    environment = os.environ.get("ENVIRONMENT", "").strip()
    if environment and _ENVIRONMENT_ATTRIBUTE not in _env_resource_attributes():
        attributes[_ENVIRONMENT_ATTRIBUTE] = environment
    return attributes


def _configure_sdk(runtime: Runtime) -> None:
    """Configure SDK providers, disabling OTLP export if the SDK rejects the exporter settings."""

    def _configure() -> None:
        OpenTelemetryConfigurator().configure(
            resource_attributes=_resource_attributes(runtime),
            setup_logging_handler=False,
            # Run AttributeNamespaceProcessor ahead of export processors, even when OTLP export is disabled.
            log_record_processors=[AttributeNamespaceProcessor()],
            export_log_record_processor=runtime.log_record_processor,
        )

    try:
        _configure()
    except RuntimeError as exc:
        # OpenTelemetryConfigurator validates protocol names and installed exporter packages before
        # setting any global provider, so calling _configure() again with exporters set to "none" is safe.
        _warn_once(
            "sdk_configuration",
            f"the OpenTelemetry SDK rejected the exporter settings ({exc}); continuing with export "
            "disabled and logs on stdout only. http/protobuf is the only supported OTLP protocol.",
        )
        for name in (OTEL_LOGS_EXPORTER, OTEL_TRACES_EXPORTER, OTEL_METRICS_EXPORTER):
            os.environ[name] = "none"
        _configure()


def configure_otel(
    service_name: str | None = None,
    *,
    runtime: RuntimeName | None = None,
) -> LoggerProvider:
    """Configure OpenTelemetry logging and auto-instrumentation for the active runtime.

    On web services and scripts, call this once at startup before importing or
    instantiating frameworks such as `FastAPI()`, because instrumentors patch
    classes at import time. On AWS Lambda, call this at the bottom of the
    handler module after defining the handler function so the runtime can wrap
    it.

    Without both an OTLP endpoint and a service name, OTLP log export defaults
    to `"none"` and application logs go to stdout only. Repeat calls in the
    same process return the existing `LoggerProvider` without reconfiguring the
    SDK, and on Lambda will still wrap the handler if an earlier call ran
    before the handler existed.

    Args:
        service_name: Fallback `service.name` when neither `OTEL_SERVICE_NAME`
            nor `service.name` in `OTEL_RESOURCE_ATTRIBUTES` is set.
        runtime: Explicit `"lambda"`, `"ecs"`, or `"local"` override. When
            `None`, detects the runtime from AWS environment variables.

    Returns:
        The process-wide `LoggerProvider` attached to Python's root logger.

    Raises:
        ValueError: If `runtime` is not a supported runtime name.
        ImportError: On the `"lambda"` runtime when
            `opentelemetry-instrumentation-aws-lambda` from the
            `otel_native_lambda` extra is not installed.
    """
    resolved_runtime = detect_runtime(runtime)
    if _state.runtime is not None and _state.logger_provider is not None:
        _state.runtime.on_reconfigure()
        return _state.logger_provider

    active = _RUNTIMES[resolved_runtime](service_name=_resolve_service_name(service_name))
    endpoint_configured = has_otlp_logs_endpoint()
    if not endpoint_configured:
        _warn_once("endpoint", active.missing_endpoint_message)
    elif active.service_name is None:
        _warn_once(
            "service_name",
            "service_name is not set (neither OTEL_SERVICE_NAME nor passed to configure_otel()); the gateway "
            "drops logs with no allow-listed service.name, so OTLP log export defaults to off and logs stay on stdout.",
        )
    active.apply_env_defaults(can_export=endpoint_configured and active.service_name is not None)

    # Reload propagators from OTEL_PROPAGATORS because importing opentelemetry.propagate
    # before configure_otel() initializes the global textmap before defaults are set.
    propagate.set_global_textmap(propagate._load_propagators())  # noqa: SLF001

    _configure_sdk(active)

    provider = cast("LoggerProvider", get_logger_provider())
    _state.root_logging_handler = _install_root_handler(provider)
    _state.logger_provider = provider
    _state.runtime = active

    instrument_installed_libraries(skip=active.self_instrumented)
    active.post_configure(provider)
    return provider


__all__ = ["RuntimeName", "configure_otel", "detect_runtime"]
