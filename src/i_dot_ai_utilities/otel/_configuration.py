"""Native OpenTelemetry SDK configuration for AWS Lambda and local execution."""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import sys
import warnings
from typing import TYPE_CHECKING, Literal, cast

from opentelemetry import propagate
from opentelemetry._logs import get_logger_provider
from opentelemetry.distro import OpenTelemetryConfigurator
from opentelemetry.environment_variables import (
    OTEL_LOGS_EXPORTER,
    OTEL_METRICS_EXPORTER,
    OTEL_TRACES_EXPORTER,
)
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk.environment_variables import (
    OTEL_EXPORTER_OTLP_PROTOCOL,
    OTEL_SERVICE_NAME,
)
from opentelemetry.sdk.extension.aws.resource import AwsLambdaResourceDetector

from i_dot_ai_utilities.otel._export import (
    ExportCircuitBreaker,
    TrackedBatchLogRecordProcessor,
    TrackedLogRecordExporter,
    resolve_circuit_breaker_seconds,
)
from i_dot_ai_utilities.otel._lambda import (
    apply_lambda_env_defaults,
    has_otlp_logs_endpoint,
    instrument_lambda_runtime,
    remove_lambda_runtime_stdout_handlers,
)

if TYPE_CHECKING:
    from opentelemetry.sdk._logs import LogRecordProcessor
    from opentelemetry.sdk._logs.export import LogRecordExporter

RuntimeName = Literal["lambda", "local"]

_INTERNAL_OTEL_LOGGER_PREFIX = "opentelemetry"

_configured: bool = False
_logger_provider: LoggerProvider | None = None
_root_logging_handler: LoggingHandler | None = None
_tracked_processor: TrackedBatchLogRecordProcessor | None = None
_circuit_breaker: ExportCircuitBreaker | None = None
_missing_service_name_warned: bool = False
_missing_endpoint_warned: bool = False


class _ExcludeInternalOtelFilter(logging.Filter):
    """Filter out ``opentelemetry`` internal logs so exporter errors cannot recurse."""

    def filter(self, record: logging.LogRecord) -> bool:
        name = record.name or ""
        return not (name == _INTERNAL_OTEL_LOGGER_PREFIX or name.startswith(f"{_INTERNAL_OTEL_LOGGER_PREFIX}."))


def detect_runtime(runtime: RuntimeName | None = None) -> RuntimeName:
    """Resolve the active runtime from an explicit override or AWS environment variables.

    Container follow-ups will check ``AWS_BATCH_JOB_ID`` before
    ``ECS_CONTAINER_METADATA_URI_V4`` here; I-459 implements only ``lambda``
    and ``local``.
    """
    if runtime is not None:
        if runtime not in {"lambda", "local"}:
            msg = f"Unsupported runtime {runtime!r}; expected 'lambda' or 'local'."
            raise ValueError(msg)
        return runtime

    if os.environ.get("AWS_LAMBDA_FUNCTION_NAME", "").strip():
        return "lambda"
    return "local"


def _resolve_service_name(service_name: str | None) -> str | None:
    """Return explicit ``service_name``, else ``OTEL_SERVICE_NAME``, else warn once."""
    global _missing_service_name_warned  # noqa: PLW0603

    explicit = (service_name or "").strip()
    if explicit:
        return explicit

    from_env = os.environ.get(OTEL_SERVICE_NAME, "").strip()
    if from_env:
        return from_env

    if not _missing_service_name_warned:
        sys.stderr.write(
            "i_dot_ai_utilities.otel: service_name is not set (neither passed to "
            "configure_otel() nor set in OTEL_SERVICE_NAME); the gateway drops "
            "logs whose service.name is not on the allow-list.\n"
        )
        _missing_service_name_warned = True
    return None


def _warn_missing_endpoint_once(resolved_runtime: RuntimeName) -> None:
    global _missing_endpoint_warned  # noqa: PLW0603

    if _missing_endpoint_warned:
        return
    if resolved_runtime == "lambda":
        sys.stderr.write(
            "i_dot_ai_utilities.otel: OTEL_EXPORTER_OTLP_ENDPOINT / "
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT is not set on Lambda; "
            "continuing with OTLP log export disabled.\n"
        )
    else:
        sys.stderr.write(
            "i_dot_ai_utilities.otel: OTEL_EXPORTER_OTLP_ENDPOINT / "
            "OTEL_EXPORTER_OTLP_LOGS_ENDPOINT is not set; using console log exporter.\n"
        )
    _missing_endpoint_warned = True


def _apply_local_env_defaults(*, endpoint_configured: bool) -> None:
    os.environ.setdefault(OTEL_TRACES_EXPORTER, "none")
    os.environ.setdefault(OTEL_METRICS_EXPORTER, "none")
    os.environ.setdefault(OTEL_LOGS_EXPORTER, "otlp" if endpoint_configured else "console")
    os.environ.setdefault(OTEL_EXPORTER_OTLP_PROTOCOL, "http/protobuf")


def _build_resource_attributes(
    *,
    resolved_service_name: str | None,
    resolved_runtime: RuntimeName,
) -> dict[str, str | int | float | bool]:
    attrs: dict[str, str | int | float | bool] = {}
    if resolved_runtime == "lambda":
        detected = AwsLambdaResourceDetector().detect()
        for key, value in detected.attributes.items():
            if isinstance(value, str | int | float | bool):
                attrs[key] = value

    # Explicit ``service_name`` takes highest precedence over ``OTEL_SERVICE_NAME``
    # and ``OTEL_RESOURCE_ATTRIBUTES`` because ``Resource.create(attrs)`` merges
    # ``attrs`` on top of ``OTELResourceDetector``.
    if resolved_service_name:
        attrs["service.name"] = resolved_service_name

    return attrs


def _build_sdk_logging_handler(provider: LoggerProvider) -> LoggingHandler:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="`LoggingHandler` in `opentelemetry-sdk` is deprecated.*",
            category=DeprecationWarning,
        )
        handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
    handler.addFilter(_ExcludeInternalOtelFilter())
    return handler


def _install_root_handler_and_configure_provider(
    provider: LoggerProvider,
    resolved_runtime: RuntimeName,
) -> LoggingHandler:
    if resolved_runtime == "lambda":
        # AWS Lambda freezes and terminates processes outside invocation boundaries;
        # an ``atexit`` shutdown would bypass the outage circuit breaker and drain
        # queued records with a blocking export on exit.
        at_exit = getattr(provider, "_at_exit_handler", None)
        if at_exit is not None:
            atexit.unregister(at_exit)
            provider._at_exit_handler = None  # noqa: SLF001

    handler = _build_sdk_logging_handler(provider)
    root_logger = logging.getLogger()
    if resolved_runtime == "lambda":
        remove_lambda_runtime_stdout_handlers(root_logger)
    root_logger.addHandler(handler)
    if root_logger.level == logging.WARNING:
        # Python's root logger defaults to WARNING; lower it to INFO when still
        # untouched so standard ``logging.getLogger(__name__).info(...)`` works
        # out of the box without overriding an application-configured level.
        root_logger.setLevel(logging.INFO)
    return handler


def configure_otel(
    service_name: str | None = None,
    *,
    runtime: RuntimeName | None = None,
) -> LoggerProvider:
    """Configure native OpenTelemetry providers, logging handler, and Lambda instrumentation.

    Idempotent within a process. On Lambda, calling ``configure_otel`` again
    after defining the handler still ensures the handler is wrapped once if it
    was not yet defined on an earlier call.
    """
    global _circuit_breaker, _configured, _logger_provider, _root_logging_handler, _tracked_processor  # noqa: PLW0603

    resolved_runtime = detect_runtime(runtime)
    if _configured and _logger_provider is not None:
        if resolved_runtime == "lambda" and _circuit_breaker is not None:
            remove_lambda_runtime_stdout_handlers()
            instrument_lambda_runtime(
                processor=_tracked_processor,
                breaker=_circuit_breaker,
            )
        return _logger_provider

    resolved_service_name = _resolve_service_name(service_name)
    endpoint_configured = has_otlp_logs_endpoint()
    if not endpoint_configured:
        _warn_missing_endpoint_once(resolved_runtime)

    if resolved_runtime == "lambda":
        apply_lambda_env_defaults(endpoint_configured=endpoint_configured)
    else:
        _apply_local_env_defaults(endpoint_configured=endpoint_configured)

    # Refresh global propagators from ``OTEL_PROPAGATORS`` so Lambda's X-Ray +
    # W3C default takes effect even if ``opentelemetry.propagate`` was imported
    # before ``configure_otel()`` ran.
    propagate.set_global_textmap(propagate._load_propagators())  # noqa: SLF001

    breaker = ExportCircuitBreaker(open_seconds=resolve_circuit_breaker_seconds())
    created_processors: list[TrackedBatchLogRecordProcessor] = []

    def _wrap_log_exporter(exporter: LogRecordExporter) -> LogRecordProcessor:
        tracked_exporter = TrackedLogRecordExporter(exporter, breaker)
        processor = TrackedBatchLogRecordProcessor(tracked_exporter)
        created_processors.append(processor)
        return processor

    resource_attributes = _build_resource_attributes(
        resolved_service_name=resolved_service_name,
        resolved_runtime=resolved_runtime,
    )

    OpenTelemetryConfigurator().configure(
        resource_attributes=resource_attributes,
        setup_logging_handler=False,
        export_log_record_processor=_wrap_log_exporter,
    )

    provider = cast("LoggerProvider", get_logger_provider())
    handler = _install_root_handler_and_configure_provider(provider, resolved_runtime)

    _logger_provider = provider
    _root_logging_handler = handler
    _tracked_processor = created_processors[0] if created_processors else None
    _circuit_breaker = breaker
    _configured = True

    if resolved_runtime == "lambda":
        instrument_lambda_runtime(
            processor=_tracked_processor,
            breaker=breaker,
        )

    return provider


def _reset_for_tests() -> None:
    """Reset module state and uninstrument Lambda/botocore wrappers (tests only)."""
    global _circuit_breaker, _configured, _logger_provider, _missing_endpoint_warned  # noqa: PLW0603
    global _missing_service_name_warned, _root_logging_handler, _tracked_processor  # noqa: PLW0603

    root_logger = logging.getLogger()
    if _root_logging_handler is not None and _root_logging_handler in root_logger.handlers:
        root_logger.removeHandler(_root_logging_handler)

    if _logger_provider is not None and hasattr(_logger_provider, "shutdown"):
        with contextlib.suppress(Exception):
            _logger_provider.shutdown()

    for instrumentor_import in (
        ("opentelemetry.instrumentation.aws_lambda", "AwsLambdaInstrumentor"),
        ("opentelemetry.instrumentation.botocore", "BotocoreInstrumentor"),
    ):
        with contextlib.suppress(Exception):
            mod = __import__(instrumentor_import[0], fromlist=[instrumentor_import[1]])
            cls = getattr(mod, instrumentor_import[1])
            inst = cls()
            if inst.is_instrumented_by_opentelemetry:
                inst.uninstrument()

    _configured = False
    _logger_provider = None
    _root_logging_handler = None
    _tracked_processor = None
    _circuit_breaker = None
    _missing_service_name_warned = False
    _missing_endpoint_warned = False


__all__ = [
    "RuntimeName",
    "_reset_for_tests",
    "configure_otel",
    "detect_runtime",
]
