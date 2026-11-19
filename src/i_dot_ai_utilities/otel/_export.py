"""Wrap OTLP log exporters and processors with EMF failure reporting and the Lambda outage circuit breaker."""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from copy import copy
from typing import TYPE_CHECKING, Final

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
)
from opentelemetry.sdk.resources import Resource

from i_dot_ai_utilities.otel._environment import env_number

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

ENV_CIRCUIT_BREAKER_SECONDS: Final[str] = "I_DOT_AI_OTEL_LAMBDA_CIRCUIT_BREAKER_SECONDS"
DEFAULT_CIRCUIT_BREAKER_SECONDS: Final[float] = 30.0
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


class FailureReportingExporter(LogRecordExporter):
    """Invoke `on_failure` whenever the delegate OTLP exporter returns `FAILURE` or raises.

    Long-running container runtimes export on the SDK's background thread
    outside the request path, so they report failures and let the SDK retry
    rather than opening a circuit breaker.
    """

    def __init__(self, delegate: LogRecordExporter, on_failure: Callable[[str], None]) -> None:
        self._delegate = delegate
        self._on_failure = on_failure

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        """Export `batch` through the delegate, converting uncaught exceptions into `FAILURE`."""
        try:
            result = self._delegate.export(batch)
        except Exception:  # noqa: BLE001
            result = LogRecordExportResult.FAILURE
        if result is not LogRecordExportResult.SUCCESS:
            self._on_failure("export_failure")
        return result

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._delegate.force_flush(timeout_millis))

    def shutdown(self) -> None:
        self._delegate.shutdown()


class LambdaLogRecordProcessor(LogRecordProcessor):
    """Buffer log records in a bounded deque and export synchronously at Lambda invocation boundaries.

    Avoids `BatchLogRecordProcessor`'s background timer thread, which Lambda
    freezes mid-flight between invocations and which would wake on batch-size
    thresholds while `ExportCircuitBreaker` is open. `on_emit` appends directly
    to a bounded `collections.deque` without locks, and `force_flush` drains
    batches synchronously while respecting the circuit breaker.
    """

    def __init__(
        self,
        exporter: LogRecordExporter,
        breaker: ExportCircuitBreaker,
    ) -> None:
        max_queue_size = _resolve_positive_int(OTEL_BLRP_MAX_QUEUE_SIZE, DEFAULT_LAMBDA_QUEUE_SIZE)
        max_batch_size = _resolve_positive_int(OTEL_BLRP_MAX_EXPORT_BATCH_SIZE, DEFAULT_LAMBDA_EXPORT_BATCH_SIZE)
        self._exporter = exporter
        self._breaker = breaker
        self._queue: deque[ReadableLogRecord] = deque(maxlen=max_queue_size)
        self._max_export_batch_size = min(max_batch_size, max_queue_size)
        self._export_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._failed_since_reset = False
        self._shutdown = False

    @property
    def has_pending(self) -> bool:
        """Return `True` when at least one record is waiting in the queue."""
        return bool(self._queue)

    def reset_export_window(self) -> None:
        """Clear the per-flush failure flag before starting a synchronous Lambda flush."""
        with self._state_lock:
            self._failed_since_reset = False

    def consume_export_failure(self) -> bool:
        """Return and clear whether any batch failed since the last `reset_export_window` call."""
        with self._state_lock:
            failed = self._failed_since_reset
            self._failed_since_reset = False
            return failed

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

    def _export_batch(self, batch: Sequence[ReadableLogRecord]) -> bool:
        with suppress_instrumentation():
            try:
                result = self._exporter.export(batch)
            except Exception:  # noqa: BLE001
                result = LogRecordExportResult.FAILURE

        if result is LogRecordExportResult.SUCCESS:
            self._breaker.record_success()
            return True

        with self._state_lock:
            self._failed_since_reset = True
        self._breaker.record_failure()
        return False

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        """Drain queued batches in FIFO order, stopping immediately if the circuit breaker opens."""
        if self._shutdown:
            return False
        with self._export_lock:
            while self._queue:
                # Fail remaining batches once the breaker opens so an outage costs at most
                # one exporter timeout per invocation rather than one per queued batch.
                if self._breaker.is_open():
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


__all__ = [
    "DEFAULT_CIRCUIT_BREAKER_SECONDS",
    "ENV_CIRCUIT_BREAKER_SECONDS",
    "ExportCircuitBreaker",
    "FailureReportingExporter",
    "LambdaLogRecordProcessor",
    "resolve_circuit_breaker_seconds",
]
