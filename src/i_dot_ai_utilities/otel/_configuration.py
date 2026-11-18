"""``configure_otel``: one entry point that sets up OpenTelemetry for the detected runtime.

Every runtime runs the same sequence; :class:`~i_dot_ai_utilities.otel._runtime.Runtime`
subclasses customise individual steps:

1. Apply environment defaults (``setdefault``, so explicit variables win).
2. Build resource attributes (runtime detector, service name, environment).
3. Configure the SDK through ``OpenTelemetryConfigurator``, with the ``i_ai``
   attribute namespace check ahead of the runtime's export processor.
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


def _env_resource_attributes() -> dict[str, AttributeValue]:
    """Return the attributes set by ``OTEL_SERVICE_NAME`` and ``OTEL_RESOURCE_ATTRIBUTES``."""
    return dict(OTELResourceDetector().detect().attributes)


def _resolve_service_name(service_name: str | None) -> str | None:
    """Return the environment's service name, else ``service_name``, else None.

    The environment wins because the shared Terraform modules set
    ``OTEL_SERVICE_NAME`` to the name on the gateway's allow-list, and the
    gateway silently drops any other name.
    """
    from_env = str(_env_resource_attributes().get("service.name", "")).strip()
    return from_env or (service_name or "").strip() or None


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
    # ``Resource.create(attributes)`` merges these on top of ``OTEL_RESOURCE_ATTRIBUTES`` and
    # ``OTEL_SERVICE_NAME``. The resolved name already prefers the environment's own.
    if runtime.service_name:
        attributes["service.name"] = runtime.service_name
    # Loki indexes this label. i.AI services already set ``ENVIRONMENT``, so it is the default.
    environment = os.environ.get("ENVIRONMENT", "").strip()
    if environment and _ENVIRONMENT_ATTRIBUTE not in _env_resource_attributes():
        attributes[_ENVIRONMENT_ATTRIBUTE] = environment
    return attributes


def _configure_sdk(runtime: Runtime) -> None:
    """Configure the SDK providers, falling back to no export if the SDK rejects the exporter settings."""

    def _configure() -> None:
        OpenTelemetryConfigurator().configure(
            resource_attributes=_resource_attributes(runtime),
            setup_logging_handler=False,
            # Runs before the export processors, and still runs when there are none.
            log_record_processors=[AttributeNamespaceProcessor()],
            export_log_record_processor=runtime.log_record_processor,
        )

    try:
        _configure()
    except RuntimeError as exc:
        # Raised for an unsupported protocol or a missing exporter package (``http/json`` and
        # ``grpc`` both do), before the SDK sets any provider, so configuring again is safe.
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
    """Configure OpenTelemetry logging and auto-instrumentation for the current runtime.

    Call once at start-up, before importing or creating framework objects such
    as ``FastAPI()`` so instrumentation can patch them. On Lambda, call it at
    the bottom of the handler module so the handler can be wrapped.

    ``service_name`` is a fallback: ``OTEL_SERVICE_NAME`` (or ``service.name``
    in ``OTEL_RESOURCE_ATTRIBUTES``) wins when set. With an endpoint but no
    service name, OTLP export is turned off and logs go to stdout only.

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
    elif active.service_name is None:
        _warn_once(
            "service_name",
            "service_name is not set (neither OTEL_SERVICE_NAME nor passed to configure_otel()); the gateway "
            "drops logs with no allow-listed service.name, so OTLP log export defaults to off and logs stay on stdout.",
        )
    active.apply_env_defaults(can_export=endpoint_configured and active.service_name is not None)

    # Reload propagators from ``OTEL_PROPAGATORS`` so the AWS X-Ray + W3C default takes
    # effect even if ``opentelemetry.propagate`` was imported before configure_otel() ran.
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
