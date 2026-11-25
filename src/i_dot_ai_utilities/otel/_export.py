"""Wrap OTLP log exporters and processors with EMF failure reporting and the Lambda outage circuit breaker."""

from __future__ import annotations

import contextlib
import os
import threading
import time
import weakref
from collections import deque
from copy import copy
from typing import TYPE_CHECKING, Any, ClassVar, Final

from opentelemetry.context import Context
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.sdk._logs import LogRecordProcessor, ReadableLogRecord, ReadWriteLogRecord
from opentelemetry.sdk._logs.export import (
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.environment_variables import (
    OTEL_BLRP_MAX_EXPORT_BATCH_SIZE,
    OTEL_BLRP_MAX_QUEUE_SIZE,
    OTEL_BSP_MAX_EXPORT_BATCH_SIZE,
    OTEL_BSP_MAX_QUEUE_SIZE,
)
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import ReadableSpan, SpanProcessor
from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

from i_dot_ai_utilities.otel._environment import env_number

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

ENV_CIRCUIT_BREAKER_SECONDS: Final[str] = "I_DOT_AI_OTEL_LAMBDA_CIRCUIT_BREAKER_SECONDS"
DEFAULT_CIRCUIT_BREAKER_SECONDS: Final[float] = 30.0
ENV_MAX_CONNECTION_AGE_SECONDS: Final[str] = "I_DOT_AI_OTEL_MAX_CONNECTION_AGE_SECONDS"
DEFAULT_MAX_CONNECTION_AGE_SECONDS: Final[float] = 300.0
DEFAULT_LAMBDA_QUEUE_SIZE: Final[int] = 2048
DEFAULT_LAMBDA_EXPORT_BATCH_SIZE: Final[int] = 512


def _resolve_positive_int(name: str, default: int) -> int:
    """Return a positive integer from environment variable `name`, or `default` when unset or invalid."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def resolve_circuit_breaker_seconds() -> float:
    """Read `I_DOT_AI_OTEL_LAMBDA_CIRCUIT_BREAKER_SECONDS`, defaulting to 30.0 seconds."""
    return env_number(ENV_CIRCUIT_BREAKER_SECONDS, DEFAULT_CIRCUIT_BREAKER_SECONDS)


def resolve_max_connection_age_seconds() -> float:
    """Read `I_DOT_AI_OTEL_MAX_CONNECTION_AGE_SECONDS`, defaulting to 300.0 seconds.

    Set to `0` to disable periodic HTTP connection recycling.
    """
    return env_number(ENV_MAX_CONNECTION_AGE_SECONDS, DEFAULT_MAX_CONNECTION_AGE_SECONDS)


def close_exporter_transport(exporter: object) -> None:
    """Close pooled HTTP connections held by `exporter` so the next export opens a fresh socket.

    `requests.Session.close()` closes idle sockets in its connection pool while
    remaining usable for subsequent requests.
    """
    client = getattr(exporter, "_client", None)
    transport = getattr(client, "_transport", None)
    close = getattr(transport, "close", None)
    if callable(close):
        with contextlib.suppress(Exception):
            close()


class ExportCircuitBreaker:
    """Thread-safe monotonic-clock circuit breaker for synchronous Lambda log flushes.

    Opens for `open_seconds` on the monotonic clock after an export or flush
    failure so subsequent batches and invocations fail fast without waiting on
    network timeouts. Guards all state transitions with a lock because the
    flush worker thread and the Lambda main thread share one instance.
    """

    def __init__(
        self,
        *,
        open_seconds: float = DEFAULT_CIRCUIT_BREAKER_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._open_seconds = max(open_seconds, 0.0)
        self._clock = clock
        self._open_until: float | None = None
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        """Return `True` while within the open window, clearing expired open state once it elapses."""
        with self._lock:
            if self._open_until is None:
                return False
            if self._clock() < self._open_until:
                return True
            self._open_until = None
            return False

    def record_success(self) -> None:
        """Close the breaker after a batch exports cleanly."""
        with self._lock:
            self._open_until = None

    def record_failure(self) -> None:
        """Open the breaker, or extend an existing open window to `open_seconds` from now."""
        with self._lock:
            self._open_until = self._clock() + self._open_seconds


class _FailureReportingExporter:
    """Invoke `on_failure` whenever the delegate OTLP exporter returns `FAILURE` or raises.

    Long-running container runtimes export on the SDK's background thread
    outside the request path, so they report failures and let the SDK retry
    rather than opening a circuit breaker. When `max_connection_age_seconds` is
    positive, this exporter periodically closes the underlying HTTP transport
    so active HTTP/1.1 keep-alive connections behind an L4 NLB rebalance across
    newly scaled-out collector tasks. It also closes any inherited transport
    after `os.fork()` so preforked workers do not share TLS sockets.
    """

    _result_type: ClassVar[Any]

    def __init__(
        self,
        delegate: Any,
        on_failure: Callable[[str], None],
        *,
        max_connection_age_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._delegate = delegate
        self._on_failure = on_failure
        self._max_connection_age_seconds = max(max_connection_age_seconds, 0.0)
        self._clock = clock
        self._connection_opened_at: float | None = None
        if hasattr(os, "register_at_fork"):
            weak_self = weakref.ref(self)

            def _after_fork() -> None:
                instance = weak_self()
                if instance is not None:
                    # Use a weak reference so register_at_fork does not pin exporter instances forever.
                    instance._reset_after_fork()  # noqa: SLF001

            os.register_at_fork(after_in_child=_after_fork)

    def _reset_after_fork(self) -> None:
        close_exporter_transport(self._delegate)
        self._connection_opened_at = None

    def _maybe_recycle_connection(self) -> None:
        if self._max_connection_age_seconds <= 0.0:
            return
        now = self._clock()
        if self._connection_opened_at is None:
            self._connection_opened_at = now
            return
        if (now - self._connection_opened_at) >= self._max_connection_age_seconds:
            close_exporter_transport(self._delegate)
            self._connection_opened_at = now

    def export(self, batch: Sequence[Any]) -> Any:
        """Export `batch` through the delegate, converting uncaught exceptions into `FAILURE`."""
        self._maybe_recycle_connection()
        try:
            result = self._delegate.export(batch)
        except Exception:  # noqa: BLE001
            result = self._result_type.FAILURE
        if result is not self._result_type.SUCCESS:
            self._on_failure("export_failure")
        return result

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._delegate.force_flush(timeout_millis))

    def shutdown(self) -> None:
        self._delegate.shutdown()


class FailureReportingExporter(_FailureReportingExporter, LogRecordExporter):
    """Report failed OTLP log exports. See `_FailureReportingExporter`."""

    _result_type = LogRecordExportResult


class FailureReportingSpanExporter(_FailureReportingExporter, SpanExporter):
    """Report failed OTLP span exports. See `_FailureReportingExporter`."""

    _result_type = SpanExportResult


class _LambdaExportProcessor:
    """Buffer telemetry in a bounded deque and export synchronously at Lambda invocation boundaries.

    Avoids the SDK batch processors' background timer thread, which Lambda
    freezes mid-flight between invocations and which would wake on batch-size
    thresholds while `ExportCircuitBreaker` is open. Subclasses append directly
    to a bounded `collections.deque` without locks, and `force_flush` drains
    batches synchronously while respecting the circuit breaker.
    """

    _result_type: ClassVar[Any]
    _queue_size_env: ClassVar[str]
    _batch_size_env: ClassVar[str]

    def __init__(
        self,
        exporter: Any,
        breaker: ExportCircuitBreaker,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        max_queue_size = _resolve_positive_int(self._queue_size_env, DEFAULT_LAMBDA_QUEUE_SIZE)
        max_batch_size = _resolve_positive_int(self._batch_size_env, DEFAULT_LAMBDA_EXPORT_BATCH_SIZE)
        self._exporter = exporter
        self._breaker = breaker
        self._clock = clock
        self._queue: deque[Any] = deque(maxlen=max_queue_size)
        self._max_export_batch_size = min(max_batch_size, max_queue_size)
        self._export_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._failed_since_reset = False
        self._deadline_exceeded = False
        self._shutdown = False

    @property
    def has_pending(self) -> bool:
        """Return `True` when at least one item is waiting in the queue."""
        return bool(self._queue)

    def reset_export_window(self) -> None:
        """Clear per-flush outcome flags before starting a synchronous Lambda flush."""
        with self._state_lock:
            self._failed_since_reset = False
            self._deadline_exceeded = False

    def consume_export_failure(self) -> bool:
        """Return and clear whether any batch failed since the last `reset_export_window` call."""
        with self._state_lock:
            failed = self._failed_since_reset
            self._failed_since_reset = False
            return failed

    def consume_deadline_exceeded(self) -> bool:
        """Return and clear whether `force_flush` stopped on its deadline since the last reset."""
        with self._state_lock:
            exceeded = self._deadline_exceeded
            self._deadline_exceeded = False
            return exceeded

    def _export_batch(self, batch: Sequence[Any]) -> bool:
        with suppress_instrumentation():
            try:
                result = self._exporter.export(batch)
            except Exception:  # noqa: BLE001
                result = self._result_type.FAILURE

        if result is self._result_type.SUCCESS:
            self._breaker.record_success()
            return True

        with self._state_lock:
            self._failed_since_reset = True
        self._breaker.record_failure()
        return False

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        """Drain queued batches in FIFO order until empty, the breaker opens, or `timeout_millis` elapses."""
        if self._shutdown:
            return False
        deadline = self._clock() + max(timeout_millis, 0) / 1000.0
        with self._export_lock:
            while self._queue:
                # Fail remaining batches once the breaker opens so an outage costs at most
                # one exporter timeout per invocation rather than one per queued batch.
                if self._breaker.is_open():
                    return False
                if self._clock() >= deadline:
                    with self._state_lock:
                        self._deadline_exceeded = True
                    return False
                count = min(self._max_export_batch_size, len(self._queue))
                batch = [self._queue.pop() for _ in range(count)]
                if not self._export_batch(batch):
                    return False
        return True

    def shutdown(self) -> None:
        if self._shutdown:
            return
        self._shutdown = True
        self._exporter.shutdown()


class LambdaLogRecordProcessor(_LambdaExportProcessor, LogRecordProcessor):
    """Queue log records for the per-invocation Lambda flush. See `_LambdaExportProcessor`."""

    _result_type = LogRecordExportResult
    _queue_size_env = OTEL_BLRP_MAX_QUEUE_SIZE
    _batch_size_env = OTEL_BLRP_MAX_EXPORT_BATCH_SIZE

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        """Convert `log_record` to a `ReadableLogRecord` and enqueue it, evicting the oldest entry if full."""
        if self._shutdown:
            return
        resource = log_record.resource if log_record.resource is not None else Resource.create({})
        # Shallow-copy the API record with an empty Context() so buffered records do not retain
        # active span and request objects in memory across Lambda invocations.
        api_log_record = copy(log_record.log_record)
        api_log_record.context = Context()
        readable = ReadableLogRecord(
            log_record=api_log_record,
            resource=resource,
            instrumentation_scope=log_record.instrumentation_scope,
            limits=log_record.limits,
        )
        self._queue.appendleft(readable)


class LambdaSpanProcessor(_LambdaExportProcessor, SpanProcessor):
    """Queue finished spans for the per-invocation Lambda flush. See `_LambdaExportProcessor`.

    Shares the `ExportCircuitBreaker` and bounded flush worker with logs.
    """

    _result_type = SpanExportResult
    _queue_size_env = OTEL_BSP_MAX_QUEUE_SIZE
    _batch_size_env = OTEL_BSP_MAX_EXPORT_BATCH_SIZE

    def on_end(self, span: ReadableSpan) -> None:
        if self._shutdown:
            return
        self._queue.appendleft(span)


__all__ = [
    "DEFAULT_CIRCUIT_BREAKER_SECONDS",
    "DEFAULT_MAX_CONNECTION_AGE_SECONDS",
    "ENV_CIRCUIT_BREAKER_SECONDS",
    "ENV_MAX_CONNECTION_AGE_SECONDS",
    "ExportCircuitBreaker",
    "FailureReportingExporter",
    "FailureReportingSpanExporter",
    "LambdaLogRecordProcessor",
    "LambdaSpanProcessor",
    "close_exporter_transport",
    "resolve_circuit_breaker_seconds",
    "resolve_max_connection_age_seconds",
]
