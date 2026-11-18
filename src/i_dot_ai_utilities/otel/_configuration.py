"""``configure_otel``: one entry point that sets up OpenTelemetry for the detected runtime.

Every runtime runs the same sequence; :class:`~i_dot_ai_utilities.otel._runtime.Runtime`
subclasses customise individual steps:

1. Apply environment defaults (``setdefault``, so explicit variables win).
2. Build resource attributes (runtime detector, then explicit ``service_name``).
3. Configure the SDK through ``OpenTelemetryConfigurator``, wrapping the log
   exporter in the runtime's processor and the ``i_ai`` attribute namespace check.
4. Attach the OpenTelemetry logging handler to the root logger.
5. Auto-instrument every installed instrumentation package.
6. Run the runtime's post-configure hook (flush wrapper, shutdown cap, stdout).
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
from opentelemetry.sdk._configuration import _overwrite_logging_config_fns
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk.environment_variables import OTEL_SERVICE_NAME

from i_dot_ai_utilities.otel._attributes import AttributeNamespaceProcessor
from i_dot_ai_utilities.otel._ecs import EcsRuntime, is_container_runtime
from i_dot_ai_utilities.otel._environment import has_otlp_logs_endpoint
from i_dot_ai_utilities.otel._instrumentation import instrument_installed_libraries
from i_dot_ai_utilities.otel._lambda import LambdaRuntime
from i_dot_ai_utilities.otel._runtime import LocalRuntime, Runtime, RuntimeName

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LogRecordProcessor
    from opentelemetry.sdk._logs.export import LogRecordExporter
    from opentelemetry.util.types import AttributeValue

_RUNTIMES: dict[RuntimeName, type[Runtime]] = {
    "lambda": LambdaRuntime,
    "ecs": EcsRuntime,
    "local": LocalRuntime,
}

_INTERNAL_OTEL_LOGGER_PREFIX = "opentelemetry"
_NOISY_LIBRARY_LOGGERS = ("botocore", "boto3", "urllib3")


@dataclass
class _State:
    """Everything ``configure_otel`` sets up once per process."""

    runtime: Runtime | None = None
    logger_provider: LoggerProvider | None = None
    root_logging_handler: LoggingHandler | None = None
    warned: set[str] = field(default_factory=set)


_state = _State()


class _ExcludeInternalOtelFilter(logging.Filter):
    """Filter out ``opentelemetry`` internal logs so exporter errors cannot recurse."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        return not (name == _INTERNAL_OTEL_LOGGER_PREFIX or name.startswith(f"{_INTERNAL_OTEL_LOGGER_PREFIX}."))


def detect_runtime(runtime: RuntimeName | None = None) -> RuntimeName:
    """Resolve the runtime from an explicit override, else AWS environment variables.

    Lambda is checked first, then ECS (Fargate or EC2) and AWS Batch, which both
    expose ``ECS_CONTAINER_METADATA_URI_V4``; anything else is ``local``.
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
    if key in _state.warned:
        return
    _state.warned.add(key)
    sys.stderr.write(f"i_dot_ai_utilities.otel: {message}\n")


def _resolve_service_name(service_name: str | None) -> str | None:
    """Return explicit ``service_name``, else ``OTEL_SERVICE_NAME``, else warn once."""
    resolved = (service_name or "").strip() or os.environ.get(OTEL_SERVICE_NAME, "").strip()
    if not resolved:
        _warn_once(
            "service_name",
            "service_name is not set (neither passed to configure_otel() nor set in OTEL_SERVICE_NAME); "
            "the gateway drops logs whose service.name is not on the allow-list.",
        )
    return resolved or None


def _build_sdk_logging_handler(provider: LoggerProvider) -> LoggingHandler:
    # shortcut: the SDK LoggingHandler is deprecated in favour of opentelemetry-instrumentation-logging;
    # move once that package's handler is released as stable.
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
    handler = _build_sdk_logging_handler(provider)
    root_logger = logging.getLogger()
    root_logger.addHandler(handler)
    # SDK internal: keeps the handler on the root logger when the application later calls
    # ``dictConfig``, ``fileConfig`` or ``basicConfig`` (Alembic's ``env.py`` does), which
    # would otherwise remove it and stop OTLP export without any error.
    _overwrite_logging_config_fns(handler)
    if root_logger.level == logging.WARNING:
        # Python's root logger defaults to WARNING; lower it to INFO when still at that
        # default so ``logging.getLogger(__name__).info(...)`` works out of the box. An app
        # that wants WARNING must set it after configure_otel() (documented in the README).
        root_logger.setLevel(logging.INFO)
    for name in _NOISY_LIBRARY_LOGGERS:
        # Keep AWS SDK and HTTP client INFO chatter out of OTLP unless the app set a level.
        library_logger = logging.getLogger(name)
        if library_logger.level == logging.NOTSET:
            library_logger.setLevel(logging.WARNING)
    return handler


def _resource_attributes(runtime: Runtime) -> dict[str, AttributeValue]:
    attributes = runtime.resource_attributes()
    # ``Resource.create(attributes)`` merges these on top of ``OTEL_RESOURCE_ATTRIBUTES``
    # and ``OTEL_SERVICE_NAME``, so an explicit ``service_name`` takes highest precedence.
    if runtime.service_name:
        attributes["service.name"] = runtime.service_name
    return attributes


def configure_otel(
    service_name: str | None = None,
    *,
    runtime: RuntimeName | None = None,
) -> LoggerProvider:
    """Configure OpenTelemetry logging and auto-instrumentation for the current runtime.

    Call once at start-up, before importing or creating framework objects such
    as ``FastAPI()`` so instrumentation can patch them. On Lambda, call it at
    the bottom of the handler module so the handler can be wrapped.

    Idempotent within a process: later calls return the same provider (on
    Lambda they also wrap the handler if an earlier call ran before it existed).
    """
    resolved_runtime = detect_runtime(runtime)
    if _state.runtime is not None and _state.logger_provider is not None:
        _state.runtime.on_reconfigure()
        return _state.logger_provider

    active = _RUNTIMES[resolved_runtime](service_name=_resolve_service_name(service_name))
    endpoint_configured = has_otlp_logs_endpoint()
    if not endpoint_configured:
        _warn_once("endpoint", active.missing_endpoint_message)
    active.apply_env_defaults(endpoint_configured=endpoint_configured)

    # Reload propagators from ``OTEL_PROPAGATORS`` so the AWS X-Ray + W3C default takes
    # effect even if ``opentelemetry.propagate`` was imported before configure_otel() ran.
    propagate.set_global_textmap(propagate._load_propagators())  # noqa: SLF001

    def _log_record_processor(exporter: LogRecordExporter) -> LogRecordProcessor:
        return AttributeNamespaceProcessor(active.log_record_processor(exporter))

    OpenTelemetryConfigurator().configure(
        resource_attributes=_resource_attributes(active),
        setup_logging_handler=False,
        export_log_record_processor=_log_record_processor,
    )

    provider = cast("LoggerProvider", get_logger_provider())
    _state.root_logging_handler = _install_root_handler(provider)
    _state.logger_provider = provider
    _state.runtime = active

    instrument_installed_libraries(skip=active.self_instrumented)
    active.post_configure(provider)
    return provider


__all__ = ["RuntimeName", "configure_otel", "detect_runtime"]
