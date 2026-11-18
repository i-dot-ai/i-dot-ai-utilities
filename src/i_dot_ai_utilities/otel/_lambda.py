"""AWS Lambda runtime: env defaults, resource, circuit-breaker export, and per-invocation log flushing.

Lambda freezes the process as soon as the handler returns, so the SDK's
background export thread cannot be relied on. Instead the handler is wrapped
to flush logs synchronously, bounded by the export timeout and skipped while
the outage circuit breaker is open.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import os
import sys
import threading
from typing import TYPE_CHECKING, Any, Final

from opentelemetry._logs import get_logger_provider
from opentelemetry.sdk.environment_variables import (
    OTEL_BLRP_MAX_EXPORT_BATCH_SIZE,
    OTEL_BLRP_MAX_QUEUE_SIZE,
    OTEL_BLRP_SCHEDULE_DELAY,
    OTEL_EXPORTER_OTLP_LOGS_TIMEOUT,
    OTEL_EXPORTER_OTLP_TIMEOUT,
)
from opentelemetry.sdk.extension.aws.resource import AwsLambdaResourceDetector
from wrapt import wrap_function_wrapper

from i_dot_ai_utilities.otel._emf import emit_export_failure
from i_dot_ai_utilities.otel._environment import env_number, keep_stdout_logs, logs_export_disabled
from i_dot_ai_utilities.otel._export import (
    ExportCircuitBreaker,
    TrackedBatchLogRecordProcessor,
    TrackedLogRecordExporter,
    resolve_circuit_breaker_seconds,
)
from i_dot_ai_utilities.otel._runtime import Runtime, apply_aws_trace_env_defaults, unregister_sdk_atexit

if TYPE_CHECKING:
    from collections.abc import Callable

    from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
    from opentelemetry.sdk._logs.export import LogRecordExporter
    from opentelemetry.util.types import AttributeValue

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

_inflight_flush: threading.Thread | None = None


def emit_lambda_export_failure(reason: str) -> None:
    """Emit the EMF ``ExportFailures`` line with the ``FunctionName`` dimension."""
    emit_export_failure(
        reason,
        dimension="FunctionName",
        value=os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or "unknown",
    )


def is_lambda_runtime_stdout_handler(handler: logging.Handler) -> bool:
    """Return True only for the AWS Lambda Python runtime's built-in root handlers."""
    cls = type(handler)
    module_name = getattr(cls, "__module__", "") or ""
    class_name = getattr(cls, "__name__", "") or ""
    return module_name.startswith("awslambdaric") and class_name.startswith("LambdaLoggerHandler")


def remove_lambda_runtime_stdout_handlers(
    root_logger: logging.Logger | None = None,
) -> int:
    """Remove only ``awslambdaric``'s root handlers so application logs are not duplicated.

    Keeps them unless the app opted out of stdout logs, and always when OTLP log
    export is disabled, so turning export off never turns application logging off.
    """
    if logs_export_disabled() or keep_stdout_logs():
        return 0
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
    return int(env_number(ENV_FLUSH_MARGIN_MS, DEFAULT_FLUSH_MARGIN_MS))


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


def _run_flush(processor: TrackedBatchLogRecordProcessor, timeout_ms: int, outcome: dict[str, bool]) -> None:
    try:
        # Flush through the provider so every configured exporter is flushed, not only the tracked one.
        provider = get_logger_provider()
        if hasattr(provider, "force_flush"):
            outcome["flushed"] = bool(provider.force_flush(timeout_ms))
        else:
            outcome["flushed"] = processor.force_flush(timeout_ms)
    except Exception:  # noqa: BLE001 - flush must never break the handler
        outcome["flushed"] = False


def flush_lambda_logs(  # noqa: PLR0911 - each early return is a distinct skip reason
    context: Any,
    *,
    processor: TrackedBatchLogRecordProcessor | None,
    breaker: ExportCircuitBreaker,
    failure_emitter: Callable[[str], None] = emit_lambda_export_failure,
) -> bool:
    """Flush queued OTLP logs at the end of a Lambda invocation.

    The flush runs on a worker thread and the handler waits at most the export
    timeout, so a slow DNS lookup or connect cannot hold the response beyond it.
    Returns True when a flush ran and succeeded, False when skipped or failed.
    """
    global _inflight_flush  # noqa: PLW0603

    if processor is None:
        return True

    if breaker.is_open():
        # Keep the failure metric flowing for the whole outage, not just its first flush.
        failure_emitter("breaker_open")
        return False

    if _inflight_flush is not None and _inflight_flush.is_alive():
        failure_emitter("flush_in_progress")
        return False

    timeout_ms = resolve_logs_export_timeout_ms()
    margin_ms = resolve_flush_margin_ms()
    remaining_ms = _remaining_time_millis(context)
    if remaining_ms is not None and remaining_ms < (timeout_ms + margin_ms):
        return False

    exporter = processor.exporter
    exporter.reset_window()
    outcome: dict[str, bool] = {}
    worker = threading.Thread(
        target=_run_flush,
        args=(processor, timeout_ms, outcome),
        name="i-dot-ai-otel-flush",
        daemon=True,
    )
    worker.start()
    worker.join(timeout_ms / 1000)

    if worker.is_alive():
        # Abandon the stuck export; it finishes or fails on a later thaw, and later flushes skip while it runs.
        _inflight_flush = worker
        failure_emitter("flush_timeout")
        return False

    flushed = outcome.get("flushed", False)
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


def instrument_lambda_handler(
    *,
    processor: TrackedBatchLogRecordProcessor | None,
    breaker: ExportCircuitBreaker,
    failure_emitter: Callable[[str], None] = emit_lambda_export_failure,
) -> bool:
    """Instrument the Lambda handler, then install the outer log flush wrapper around it.

    Returns True when the handler is wrapped, False when it could not be found.
    Raises ``ImportError`` when ``opentelemetry-instrumentation-aws-lambda`` is
    missing, so a Lambda deployed without the ``[otel_native_lambda]`` extra fails fast.
    """
    from opentelemetry.instrumentation.aws_lambda import (  # noqa: PLC0415
        AwsLambdaInstrumentor,
    )

    target = resolve_lambda_handler_target()
    if target is None:
        sys.stderr.write(
            "i_dot_ai_utilities.otel: ORIG_HANDLER/_HANDLER is not set; skipping Lambda handler wrapping.\n"
        )
        return False

    module_name, function_name = target
    try:
        handler_module = importlib.import_module(module_name)
    except Exception:  # noqa: BLE001 - fail-open on bad handler module
        sys.stderr.write(
            f"i_dot_ai_utilities.otel: failed to import handler module {module_name!r}; skipping handler wrapping.\n"
        )
        return False

    if not hasattr(handler_module, function_name):
        sys.stderr.write(
            f"i_dot_ai_utilities.otel: handler {function_name!r} not found on {module_name!r}; "
            "call configure_otel() after defining the handler.\n"
        )
        return False

    lambda_instrumentor = AwsLambdaInstrumentor()
    if not lambda_instrumentor.is_instrumented_by_opentelemetry:
        lambda_instrumentor.instrument()

    current_handler = getattr(handler_module, function_name, None)
    if getattr(current_handler, _LAMBDA_HANDLER_MARKER, False):
        return True

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
    return True


class LambdaRuntime(Runtime):
    """AWS Lambda: synchronous, time-bounded flush at the end of every invocation."""

    self_instrumented = frozenset({"aws-lambda"})
    missing_endpoint_message = (
        "OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_LOGS_ENDPOINT is not set on Lambda; "
        "continuing with OTLP log export disabled."
    )

    def __init__(self, *, service_name: str | None) -> None:
        super().__init__(service_name=service_name)
        self.breaker = ExportCircuitBreaker(open_seconds=resolve_circuit_breaker_seconds())
        self.processor: TrackedBatchLogRecordProcessor | None = None

    def apply_env_defaults(self, *, can_export: bool) -> None:
        super().apply_env_defaults(can_export=can_export)
        apply_aws_trace_env_defaults()
        if not os.environ.get(OTEL_EXPORTER_OTLP_TIMEOUT, "").strip():
            os.environ.setdefault(OTEL_EXPORTER_OTLP_LOGS_TIMEOUT, DEFAULT_LAMBDA_LOGS_TIMEOUT_SECONDS)
        os.environ.setdefault(OTEL_BLRP_MAX_EXPORT_BATCH_SIZE, DEFAULT_LAMBDA_MAX_EXPORT_BATCH_SIZE)
        os.environ.setdefault(OTEL_BLRP_MAX_QUEUE_SIZE, DEFAULT_LAMBDA_MAX_QUEUE_SIZE)
        os.environ.setdefault(OTEL_BLRP_SCHEDULE_DELAY, DEFAULT_LAMBDA_SCHEDULE_DELAY_MILLIS)
        os.environ.setdefault(
            OTEL_INSTRUMENTATION_AWS_LAMBDA_FLUSH_TIMEOUT,
            DEFAULT_LAMBDA_INSTRUMENTATION_FLUSH_TIMEOUT_MS,
        )

    def resource_attributes(self) -> dict[str, AttributeValue]:
        return dict(AwsLambdaResourceDetector().detect().attributes)

    def log_record_processor(self, exporter: LogRecordExporter) -> LogRecordProcessor:
        processor = TrackedBatchLogRecordProcessor(TrackedLogRecordExporter(exporter, self.breaker))
        if self.processor is None:
            self.processor = processor
        return processor

    def post_configure(self, provider: LoggerProvider) -> None:
        # Lambda freezes and terminates processes outside invocation boundaries;
        # an ``atexit`` shutdown would bypass the outage circuit breaker and drain
        # queued records with a blocking export on exit.
        unregister_sdk_atexit(provider)
        self.on_reconfigure()

    def on_reconfigure(self) -> None:
        # A second call after the handler is defined still wraps it, if the first call ran too early.
        # Stdout is only dropped once the per-invocation flush is in place, or nothing would carry the logs.
        if instrument_lambda_handler(processor=self.processor, breaker=self.breaker):
            remove_lambda_runtime_stdout_handlers()


def reset_inflight_flush() -> None:
    """Forget an abandoned flush thread (used by tests)."""
    global _inflight_flush  # noqa: PLW0603
    _inflight_flush = None


__all__ = [
    "DEFAULT_FLUSH_MARGIN_MS",
    "DEFAULT_LAMBDA_LOGS_TIMEOUT_SECONDS",
    "ENV_FLUSH_MARGIN_MS",
    "LambdaRuntime",
    "emit_lambda_export_failure",
    "flush_lambda_logs",
    "instrument_lambda_handler",
    "is_lambda_runtime_stdout_handler",
    "remove_lambda_runtime_stdout_handlers",
    "reset_inflight_flush",
    "resolve_flush_margin_ms",
    "resolve_lambda_handler_target",
    "resolve_logs_export_timeout_ms",
]
