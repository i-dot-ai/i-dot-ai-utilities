"""AWS Lambda runtime defaults, instrumentation, stdout suppression, and log flushing."""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
import sys
from typing import TYPE_CHECKING, Any, Final

from opentelemetry._logs import get_logger_provider
from opentelemetry.environment_variables import (
    OTEL_LOGS_EXPORTER,
    OTEL_METRICS_EXPORTER,
    OTEL_PROPAGATORS,
    OTEL_PYTHON_ID_GENERATOR,
    OTEL_TRACES_EXPORTER,
)
from opentelemetry.sdk.environment_variables import (
    OTEL_BLRP_MAX_EXPORT_BATCH_SIZE,
    OTEL_BLRP_MAX_QUEUE_SIZE,
    OTEL_BLRP_SCHEDULE_DELAY,
    OTEL_EXPORTER_OTLP_LOGS_TIMEOUT,
    OTEL_EXPORTER_OTLP_PROTOCOL,
    OTEL_EXPORTER_OTLP_TIMEOUT,
)
from wrapt import wrap_function_wrapper

from i_dot_ai_utilities.otel._emf import emit_lambda_export_failure

if TYPE_CHECKING:
    from collections.abc import Callable

    from i_dot_ai_utilities.otel._export import (
        ExportCircuitBreaker,
        TrackedBatchLogRecordProcessor,
    )

ENV_FLUSH_MARGIN_MS: Final[str] = "I_DOT_AI_OTEL_LAMBDA_FLUSH_MARGIN_MS"
DEFAULT_FLUSH_MARGIN_MS: Final[int] = 250
DEFAULT_LAMBDA_LOGS_TIMEOUT_SECONDS: Final[str] = "2"
DEFAULT_LAMBDA_MAX_EXPORT_BATCH_SIZE: Final[str] = "512"
DEFAULT_LAMBDA_MAX_QUEUE_SIZE: Final[str] = "2048"
# Keep the background worker delay safely above the default 30s circuit-breaker
# window so records remain queued while the breaker is open and are flushed on
# invocation boundaries instead.
DEFAULT_LAMBDA_SCHEDULE_DELAY_MILLIS: Final[str] = "60000"
DEFAULT_LAMBDA_INSTRUMENTATION_FLUSH_TIMEOUT_MS: Final[str] = "2000"
OTEL_INSTRUMENTATION_AWS_LAMBDA_FLUSH_TIMEOUT: Final[str] = "OTEL_INSTRUMENTATION_AWS_LAMBDA_FLUSH_TIMEOUT"

_LAMBDA_HANDLER_MARKER: Final[str] = "_i_dot_ai_otel_lambda_wrapped"


def has_otlp_logs_endpoint() -> bool:
    """Return True when a base or logs-specific OTLP endpoint is configured."""
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_LOGS_ENDPOINT", "").strip()
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )


def apply_lambda_env_defaults(*, endpoint_configured: bool) -> None:
    """Apply Lambda-specific OpenTelemetry environment defaults with ``setdefault``."""
    os.environ.setdefault(OTEL_TRACES_EXPORTER, "none")
    os.environ.setdefault(OTEL_METRICS_EXPORTER, "none")
    os.environ.setdefault(OTEL_LOGS_EXPORTER, "otlp" if endpoint_configured else "none")
    os.environ.setdefault(OTEL_EXPORTER_OTLP_PROTOCOL, "http/protobuf")
    if not os.environ.get(OTEL_EXPORTER_OTLP_TIMEOUT, "").strip():
        os.environ.setdefault(OTEL_EXPORTER_OTLP_LOGS_TIMEOUT, DEFAULT_LAMBDA_LOGS_TIMEOUT_SECONDS)
    os.environ.setdefault(OTEL_BLRP_MAX_EXPORT_BATCH_SIZE, DEFAULT_LAMBDA_MAX_EXPORT_BATCH_SIZE)
    os.environ.setdefault(OTEL_BLRP_MAX_QUEUE_SIZE, DEFAULT_LAMBDA_MAX_QUEUE_SIZE)
    os.environ.setdefault(OTEL_BLRP_SCHEDULE_DELAY, DEFAULT_LAMBDA_SCHEDULE_DELAY_MILLIS)
    os.environ.setdefault(
        OTEL_INSTRUMENTATION_AWS_LAMBDA_FLUSH_TIMEOUT,
        DEFAULT_LAMBDA_INSTRUMENTATION_FLUSH_TIMEOUT_MS,
    )
    os.environ.setdefault(OTEL_PYTHON_ID_GENERATOR, "xray")
    os.environ.setdefault(OTEL_PROPAGATORS, "xray,tracecontext,baggage")


def is_lambda_runtime_stdout_handler(handler: logging.Handler) -> bool:
    """Return True only for the AWS Lambda Python runtime's built-in root handlers."""
    cls = type(handler)
    module_name = getattr(cls, "__module__", "") or ""
    class_name = getattr(cls, "__name__", "") or ""
    return module_name.startswith("awslambdaric") and class_name.startswith("LambdaLoggerHandler")


def remove_lambda_runtime_stdout_handlers(
    root_logger: logging.Logger | None = None,
) -> int:
    """Remove only ``awslambdaric``'s root handlers so application logs are not duplicated."""
    root = root_logger if root_logger is not None else logging.getLogger()
    removed = 0
    for handler in list(root.handlers):
        if is_lambda_runtime_stdout_handler(handler):
            root.removeHandler(handler)
            removed += 1
    return removed


def resolve_logs_export_timeout_ms() -> int:
    """Return the configured OTLP logs export timeout in milliseconds.

    Python's OTLP HTTP exporter interprets ``OTEL_EXPORTER_OTLP_LOGS_TIMEOUT``
    and ``OTEL_EXPORTER_OTLP_TIMEOUT`` as seconds (default 10s, Lambda default 2s).
    """
    raw = (
        os.environ.get(OTEL_EXPORTER_OTLP_LOGS_TIMEOUT)
        or os.environ.get(OTEL_EXPORTER_OTLP_TIMEOUT)
        or DEFAULT_LAMBDA_LOGS_TIMEOUT_SECONDS
    )
    try:
        seconds = float(raw)
    except ValueError:
        seconds = float(DEFAULT_LAMBDA_LOGS_TIMEOUT_SECONDS)
    if seconds <= 0:
        seconds = float(DEFAULT_LAMBDA_LOGS_TIMEOUT_SECONDS)
    return int(seconds * 1000)


def resolve_flush_margin_ms() -> int:
    """Return the remaining-time safety margin in milliseconds."""
    raw = os.environ.get(ENV_FLUSH_MARGIN_MS)
    if not raw:
        return DEFAULT_FLUSH_MARGIN_MS
    try:
        margin = int(raw)
    except ValueError:
        return DEFAULT_FLUSH_MARGIN_MS
    return margin if margin >= 0 else DEFAULT_FLUSH_MARGIN_MS


def _remaining_time_millis(context: Any) -> int | None:
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(getter):
        return None
    try:
        remaining = getter()
    except Exception:  # noqa: BLE001 - defensive probe on caller context
        return None
    if isinstance(remaining, int | float):
        return int(remaining)
    return None


def flush_lambda_logs(
    context: Any,
    *,
    processor: TrackedBatchLogRecordProcessor | None,
    breaker: ExportCircuitBreaker,
    failure_emitter: Callable[[str], None] = emit_lambda_export_failure,
) -> bool:
    """Flush queued OTLP logs at the end of a Lambda invocation.

    Returns True when a flush ran and succeeded, False when skipped or failed.
    """
    if processor is None:
        return True

    if breaker.is_open():
        return False

    timeout_ms = resolve_logs_export_timeout_ms()
    margin_ms = resolve_flush_margin_ms()
    remaining_ms = _remaining_time_millis(context)
    if remaining_ms is not None and remaining_ms < (timeout_ms + margin_ms):
        return False

    exporter = processor.exporter
    exporter.reset_window()
    try:
        provider = get_logger_provider()
        if hasattr(provider, "force_flush"):
            flushed = bool(provider.force_flush(timeout_ms))
        else:
            flushed = processor.force_flush(timeout_ms)
    except Exception:  # noqa: BLE001 - flush must never break the handler
        flushed = False
        breaker.record_failure()

    exporter_failed = exporter.consume_failure()
    if not flushed or exporter_failed:
        if not exporter_failed:
            breaker.record_failure()
        reason = "export_failure" if exporter_failed else "flush_failure"
        failure_emitter(reason)
        return False

    return True


def resolve_lambda_handler_target() -> tuple[str, str] | None:
    """Resolve ``(module_name, function_name)`` from ``ORIG_HANDLER`` or ``_HANDLER``."""
    raw_handler = os.environ.get("ORIG_HANDLER") or os.environ.get("_HANDLER")
    if not raw_handler:
        return None
    normalized = raw_handler.strip().replace("/", ".")
    if "." not in normalized:
        return None
    module_name, function_name = normalized.rsplit(".", 1)
    if not module_name or not function_name:
        return None
    return module_name, function_name


def instrument_lambda_runtime(
    *,
    processor: TrackedBatchLogRecordProcessor | None,
    breaker: ExportCircuitBreaker,
    failure_emitter: Callable[[str], None] = emit_lambda_export_failure,
) -> None:
    """Instrument botocore and the Lambda handler, then install the outer log flush wrapper."""
    from opentelemetry.instrumentation.aws_lambda import (  # noqa: PLC0415
        AwsLambdaInstrumentor,
    )

    with contextlib.suppress(ModuleNotFoundError):
        from opentelemetry.instrumentation.botocore import (  # noqa: PLC0415
            BotocoreInstrumentor,
        )

        botocore_instrumentor = BotocoreInstrumentor()
        if not botocore_instrumentor.is_instrumented_by_opentelemetry:
            botocore_instrumentor.instrument()

    target = resolve_lambda_handler_target()
    if target is None:
        sys.stderr.write(
            "i_dot_ai_utilities.otel: ORIG_HANDLER/_HANDLER is not set; skipping Lambda handler wrapping.\n"
        )
        return

    module_name, function_name = target
    try:
        handler_module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - fail-open on bad handler module
        sys.stderr.write(
            f"i_dot_ai_utilities.otel: failed to import handler module {module_name!r}; skipping handler wrapping.\n"
        )
        return

    if not hasattr(handler_module, function_name):
        sys.stderr.write(
            f"i_dot_ai_utilities.otel: handler {function_name!r} not found on {module_name!r}; "
            "call configure_otel() after defining the handler.\n"
        )
        return

    lambda_instrumentor = AwsLambdaInstrumentor()
    if not lambda_instrumentor.is_instrumented_by_opentelemetry:
        lambda_instrumentor.instrument()

    current_handler = getattr(handler_module, function_name, None)
    if getattr(current_handler, _LAMBDA_HANDLER_MARKER, False):
        return

    def _flush_logs_wrapper(
        wrapped: Callable[..., Any],
        _instance: Any,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
    ) -> Any:
        context = args[1] if len(args) > 1 else kwargs.get("context")
        try:
            return wrapped(*args, **kwargs)
        finally:
            flush_lambda_logs(
                context,
                processor=processor,
                breaker=breaker,
                failure_emitter=failure_emitter,
            )

    wrap_function_wrapper(handler_module, function_name, _flush_logs_wrapper)
    wrapped_handler = getattr(handler_module, function_name, None)
    if wrapped_handler is not None:
        with contextlib.suppress(Exception):
            setattr(wrapped_handler, _LAMBDA_HANDLER_MARKER, True)


__all__ = [
    "DEFAULT_FLUSH_MARGIN_MS",
    "DEFAULT_LAMBDA_LOGS_TIMEOUT_SECONDS",
    "ENV_FLUSH_MARGIN_MS",
    "apply_lambda_env_defaults",
    "flush_lambda_logs",
    "has_otlp_logs_endpoint",
    "instrument_lambda_runtime",
    "is_lambda_runtime_stdout_handler",
    "remove_lambda_runtime_stdout_handlers",
    "resolve_flush_margin_ms",
    "resolve_lambda_handler_target",
    "resolve_logs_export_timeout_ms",
]
