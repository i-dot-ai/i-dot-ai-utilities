"""OpenTelemetry runtime support and synchronous log flushing for AWS Lambda.

AWS Lambda freezes the execution environment as soon as the handler returns, so
the SDK's background export thread can stall mid-request between invocations.
`LambdaRuntime` wraps the handler to flush logs synchronously within the export
timeout, and opens a process-local circuit breaker after failures so gateway
outages do not add latency to every invocation.
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
    OTEL_EXPORTER_OTLP_LOGS_TIMEOUT,
    OTEL_EXPORTER_OTLP_TIMEOUT,
)
from opentelemetry.sdk.extension.aws.resource import AwsLambdaResourceDetector
from wrapt import wrap_function_wrapper

from i_dot_ai_utilities.otel._emf import emit_export_failure
from i_dot_ai_utilities.otel._environment import env_number, keep_stdout_logs, logs_export_disabled
from i_dot_ai_utilities.otel._export import (
    ExportCircuitBreaker,
    LambdaLogRecordProcessor,
    resolve_circuit_breaker_seconds,
)
from i_dot_ai_utilities.otel._runtime import Runtime, apply_aws_trace_env_defaults, unregister_sdk_atexit

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
    from opentelemetry.sdk._logs.export import LogRecordExporter
    from opentelemetry.util.types import AttributeValue

ENV_FLUSH_MARGIN_MS: Final[str] = "I_DOT_AI_OTEL_LAMBDA_FLUSH_MARGIN_MS"
DEFAULT_FLUSH_MARGIN_MS: Final[int] = 250
DEFAULT_LAMBDA_LOGS_TIMEOUT_SECONDS: Final[str] = "2"
DEFAULT_LAMBDA_MAX_EXPORT_BATCH_SIZE: Final[str] = "512"
DEFAULT_LAMBDA_MAX_QUEUE_SIZE: Final[str] = "2048"
DEFAULT_LAMBDA_INSTRUMENTATION_FLUSH_TIMEOUT_MS: Final[str] = "2000"
OTEL_INSTRUMENTATION_AWS_LAMBDA_FLUSH_TIMEOUT: Final[str] = "OTEL_INSTRUMENTATION_AWS_LAMBDA_FLUSH_TIMEOUT"

_LAMBDA_HANDLER_MARKER: Final[str] = "_i_dot_ai_otel_lambda_wrapped"

_inflight_flush: threading.Thread | None = None


def emit_lambda_export_failure(reason: str) -> None:
    """Emit an EMF `ExportFailures` metric for `AWS_LAMBDA_FUNCTION_NAME`, or `"unknown"` when unset."""
    emit_export_failure(
        reason,
        dimension="FunctionName",
        value=os.environ.get("AWS_LAMBDA_FUNCTION_NAME") or "unknown",
    )


def is_lambda_runtime_stdout_handler(handler: logging.Handler) -> bool:
    """Return `True` for AWS Lambda's built-in `awslambdaric` root handlers.

    Checks `__module__` and `__name__` strings instead of `isinstance` because
    `awslambdaric` is only installed inside the managed Lambda runtime and
    defines multiple `LambdaLoggerHandler` classes.
    """
    cls = type(handler)
    module_name = getattr(cls, "__module__", "") or ""
    class_name = getattr(cls, "__name__", "") or ""
    return module_name.startswith("awslambdaric") and class_name.startswith("LambdaLoggerHandler")


def remove_lambda_runtime_stdout_handlers(
    root_logger: logging.Logger | None = None,
) -> int:
    """Detach `awslambdaric` stdout handlers from `root_logger` to avoid duplicate CloudWatch logs.

    Preserves the runtime handlers when stdout logging is enabled or when OTLP
    log export is disabled so disabling export never silences logs. Leaves
    application-added handlers untouched so custom logging setup keeps working.

    Returns:
        The number of `awslambdaric` handlers removed from `root_logger`.
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
    """Resolve the OTLP logs export timeout in milliseconds, defaulting to 2000 ms.

    Python's OTLP HTTP exporter reads `OTEL_EXPORTER_OTLP_LOGS_TIMEOUT` and
    `OTEL_EXPORTER_OTLP_TIMEOUT` in seconds rather than milliseconds, with a
    2-second default on Lambda instead of the SDK's 10-second default.
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
    """Return the `I_DOT_AI_OTEL_LAMBDA_FLUSH_MARGIN_MS` buffer in milliseconds.

    `flush_lambda_logs` requires at least the export timeout plus this margin
    on the Lambda context clock so a flush cannot run into the invocation
    timeout.
    """
    return int(env_number(ENV_FLUSH_MARGIN_MS, DEFAULT_FLUSH_MARGIN_MS))


def _remaining_time_millis(context: Any) -> int | None:
    """Read `get_remaining_time_in_millis()` from the Lambda `context`, or `None` when unavailable."""
    getter = getattr(context, "get_remaining_time_in_millis", None)
    if not callable(getter):
        return None
    try:
        remaining = getter()
    except Exception:  # noqa: BLE001
        return None
    if isinstance(remaining, int | float):
        return int(remaining)
    return None


def _run_flush(processors: Sequence[LambdaLogRecordProcessor], timeout_ms: int, outcome: dict[str, bool]) -> None:
    """Flush logs into `outcome["flushed"]`, catching all errors so a worker failure never crashes the thread."""
    try:
        # Flush via provider.force_flush when available so every registered log processor flushes alongside processors.
        provider = get_logger_provider()
        if hasattr(provider, "force_flush"):
            outcome["flushed"] = bool(provider.force_flush(timeout_ms))
        else:
            outcome["flushed"] = all(processor.force_flush(timeout_ms) for processor in processors)
    except Exception:  # noqa: BLE001
        outcome["flushed"] = False


def flush_lambda_logs(  # noqa: PLR0911
    context: Any,
    *,
    processor: LambdaLogRecordProcessor | None = None,
    processors: Sequence[LambdaLogRecordProcessor] = (),
    breaker: ExportCircuitBreaker,
    failure_emitter: Callable[[str], None] = emit_lambda_export_failure,
) -> bool:
    """Flush queued OTLP logs synchronously at the end of a Lambda invocation.

    Skips the flush immediately when no processor is configured or no records
    are queued. Otherwise skips when the circuit breaker is open, an earlier
    timed-out flush thread is still alive, or the invocation has less than the
    export timeout plus `I_DOT_AI_OTEL_LAMBDA_FLUSH_MARGIN_MS` remaining. Runs
    the flush on a daemon thread capped at the export timeout so slow DNS
    resolution or a hung socket cannot hold the response longer.

    When `force_flush` returns `False` without an exporter failure, records a
    failure on `breaker` directly so the outage window still opens.

    Returns:
        `True` when nothing needed flushing or the flush succeeded, or `False`
        when the flush was skipped or failed.
    """
    global _inflight_flush  # noqa: PLW0603

    active_processors = list(processors) if processors else ([processor] if processor is not None else [])
    if not active_processors or not any(proc.has_pending for proc in active_processors):
        return True

    if breaker.is_open():
        # Emit ExportFailures on every skipped flush so CloudWatch alarms stay active throughout an outage.
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

    for proc in active_processors:
        proc.reset_export_window()
    outcome: dict[str, bool] = {}
    worker = threading.Thread(
        target=_run_flush,
        args=(active_processors, timeout_ms, outcome),
        name="i-dot-ai-otel-flush",
        daemon=True,
    )
    worker.start()
    worker.join(timeout_ms / 1000)

    if worker.is_alive():
        # Track the stuck worker thread so subsequent invocations skip flushing until it finishes on a later thaw.
        _inflight_flush = worker
        failure_emitter("flush_timeout")
        return False

    flushed = outcome.get("flushed", False)
    exporter_failed = any(proc.consume_export_failure() for proc in active_processors)
    if not flushed or exporter_failed:
        if not exporter_failed:
            breaker.record_failure()
        reason = "export_failure" if exporter_failed else "flush_failure"
        failure_emitter(reason)
        return False

    return True


def resolve_lambda_handler_target() -> tuple[str, str] | None:
    """Parse `(module_name, function_name)` from `ORIG_HANDLER` or `_HANDLER`.

    Checks `ORIG_HANDLER` before `_HANDLER` because Lambda wrapper scripts
    reassign `_HANDLER` to themselves and preserve the user's handler in
    `ORIG_HANDLER`. Replaces path slashes with dots to support Lambda's
    `package/module.handler` syntax.
    """
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
    processor: LambdaLogRecordProcessor | None = None,
    processors: Sequence[LambdaLogRecordProcessor] = (),
    breaker: ExportCircuitBreaker,
    failure_emitter: Callable[[str], None] = emit_lambda_export_failure,
) -> bool:
    """Wrap the Lambda handler with tracing inner instrumentation and an outer log flush.

    `AwsLambdaInstrumentor` wraps the user handler first as the inner layer,
    and `_flush_logs_wrapper` wraps that instrumented callable second as the
    outer layer. Running `_flush_logs_wrapper` on the outside means its
    `finally` block calls `flush_lambda_logs` only after the invocation span
    closes and records any unhandled exception.

    Returns:
        `True` if the handler is wrapped, or `False` if `ORIG_HANDLER` and
        `_HANDLER` are unset, the module fails to import, or the function is
        not yet defined on the module.

    Raises:
        ImportError: If `opentelemetry-instrumentation-aws-lambda` from the
            `otel_native_lambda` extra is not installed.
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
    except Exception:  # noqa: BLE001
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
        """Call the instrumented handler and flush queued OTLP logs after the invocation span closes."""
        context = args[1] if len(args) > 1 else kwargs.get("context")
        try:
            return wrapped(*args, **kwargs)
        finally:
            flush_lambda_logs(
                context,
                processor=processor,
                processors=processors,
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
    """Configure OpenTelemetry for AWS Lambda with circuit-breaker export and per-invocation flushing.

    Wraps the handler directly instead of using the generic entry-point loader
    so the log flush runs outside `AwsLambdaInstrumentor`, and removes
    `awslambdaric` stdout handlers only when OTLP export and handler wrapping
    are both active.
    """

    self_instrumented = frozenset({"aws-lambda"})
    missing_endpoint_message = (
        "OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_LOGS_ENDPOINT is not set on Lambda; "
        "continuing with OTLP log export disabled."
    )

    def __init__(self, *, service_name: str | None) -> None:
        super().__init__(service_name=service_name)
        self.breaker = ExportCircuitBreaker(open_seconds=resolve_circuit_breaker_seconds())
        self.processors: list[LambdaLogRecordProcessor] = []

    def apply_env_defaults(self, *, can_export: bool) -> None:
        super().apply_env_defaults(can_export=can_export)
        apply_aws_trace_env_defaults()
        # Leave OTEL_EXPORTER_OTLP_LOGS_TIMEOUT unset when OTEL_EXPORTER_OTLP_TIMEOUT is set,
        # because the signal-specific variable would otherwise shadow the caller's general timeout.
        if not os.environ.get(OTEL_EXPORTER_OTLP_TIMEOUT, "").strip():
            os.environ.setdefault(OTEL_EXPORTER_OTLP_LOGS_TIMEOUT, DEFAULT_LAMBDA_LOGS_TIMEOUT_SECONDS)
        os.environ.setdefault(OTEL_BLRP_MAX_EXPORT_BATCH_SIZE, DEFAULT_LAMBDA_MAX_EXPORT_BATCH_SIZE)
        os.environ.setdefault(OTEL_BLRP_MAX_QUEUE_SIZE, DEFAULT_LAMBDA_MAX_QUEUE_SIZE)
        os.environ.setdefault(
            OTEL_INSTRUMENTATION_AWS_LAMBDA_FLUSH_TIMEOUT,
            DEFAULT_LAMBDA_INSTRUMENTATION_FLUSH_TIMEOUT_MS,
        )

    def resource_attributes(self) -> dict[str, AttributeValue]:
        return dict(AwsLambdaResourceDetector().detect().attributes)

    def log_record_processor(self, exporter: LogRecordExporter) -> LogRecordProcessor:
        processor = LambdaLogRecordProcessor(exporter, self.breaker)
        self.processors.append(processor)
        return processor

    def post_configure(self, provider: LoggerProvider) -> None:
        # Lambda freezes and terminates environments outside invocation boundaries. Remove the SDK
        # atexit hook so process teardown cannot bypass the circuit breaker with a blocking flush.
        unregister_sdk_atexit(provider)
        self.on_reconfigure()

    def on_reconfigure(self) -> None:
        # Retry handler wrapping if an earlier configure_otel() call ran before the handler existed.
        # Remove awslambdaric stdout handlers only after wrapping succeeds so logs are never dropped.
        if instrument_lambda_handler(processors=self.processors, breaker=self.breaker):
            remove_lambda_runtime_stdout_handlers()


def reset_inflight_flush() -> None:
    """Clear any tracked timed-out flush worker thread during test teardown."""
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
