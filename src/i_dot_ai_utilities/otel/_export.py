"""Wrap OTLP log exporters and processors with EMF failure reporting and the Lambda outage circuit breaker."""

from __future__ import annotations

import threading
import time
from copy import copy
from typing import TYPE_CHECKING, Final

from opentelemetry.context import Context
from opentelemetry.sdk._logs import LogRecordProcessor, ReadableLogRecord, ReadWriteLogRecord
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    LogRecordExportResult,
)
from opentelemetry.sdk.resources import Resource

from i_dot_ai_utilities.otel._environment import env_number

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

ENV_CIRCUIT_BREAKER_SECONDS: Final[str] = "I_DOT_AI_OTEL_LAMBDA_CIRCUIT_BREAKER_SECONDS"
DEFAULT_CIRCUIT_BREAKER_SECONDS: Final[float] = 30.0


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


class TrackedLogRecordExporter(LogRecordExporter):
    """Record batch export outcomes on `ExportCircuitBreaker` and per-flush state.

    `BatchLogRecordProcessor.force_flush()` returns `True` whenever its worker
    drains the queue in time, even if `export()` returned `FAILURE`. Tracking
    `_failed_since_reset` lets `flush_lambda_logs` tell a clean flush apart
    from a failed HTTP export.
    """

    def __init__(
        self,
        delegate: LogRecordExporter,
        breaker: ExportCircuitBreaker,
    ) -> None:
        self._delegate = delegate
        self._breaker = breaker
        self._lock = threading.Lock()
        self._failed_since_reset = False

    @property
    def breaker(self) -> ExportCircuitBreaker:
        return self._breaker

    def reset_window(self) -> None:
        """Clear the per-flush failure flag before starting a synchronous Lambda flush."""
        with self._lock:
            self._failed_since_reset = False

    def consume_failure(self) -> bool:
        """Return and clear whether any batch failed since the last `reset_window` call."""
        with self._lock:
            failed = self._failed_since_reset
            self._failed_since_reset = False
            return failed

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        """Export `batch` when the breaker is closed, opening the breaker on error or `FAILURE`."""
        # Fail remaining batches in this flush once the breaker opens so an outage costs
        # at most one exporter timeout per invocation rather than one per queued batch.
        if self._breaker.is_open():
            return LogRecordExportResult.FAILURE

        try:
            result = self._delegate.export(batch)
        except Exception:  # noqa: BLE001
            self._record_failure()
            return LogRecordExportResult.FAILURE

        if result is LogRecordExportResult.SUCCESS:
            self._breaker.record_success()
            return result

        self._record_failure()
        return LogRecordExportResult.FAILURE

    def _record_failure(self) -> None:
        with self._lock:
            self._failed_since_reset = True
        self._breaker.record_failure()

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._delegate.force_flush(timeout_millis))

    def shutdown(self) -> None:
        self._delegate.shutdown()


class TrackedBatchLogRecordProcessor(LogRecordProcessor):
    """Buffer log records without waking the background worker while `ExportCircuitBreaker` is open.

    `BatchLogRecordProcessor.on_emit` wakes its background export thread as
    soon as `max_export_batch_size` records accumulate, which would trigger
    doomed network calls mid-invocation during a gateway outage. Wrapping it on
    Lambda bypasses that wake-up while the circuit breaker is open.
    """

    def __init__(self, exporter: TrackedLogRecordExporter) -> None:
        self._exporter = exporter
        self._processor = BatchLogRecordProcessor(exporter)
        # Reach into the bounded deque behind BatchLogRecordProcessor so on_emit can enqueue
        # records without signalling the worker. TestSdkInternals verifies this attribute on upgrades.
        batch_processor = getattr(self._processor, "_batch_processor", None)
        self._queue = getattr(batch_processor, "_queue", None)

    @property
    def exporter(self) -> TrackedLogRecordExporter:
        return self._exporter

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        """Enqueue onto the bounded deque without waking the worker while the breaker is open."""
        if self._exporter.breaker.is_open() and self._queue is not None:
            # Append directly to the bounded deque so the background export worker stays asleep,
            # letting collections.deque drop the oldest record once max_queue_size is reached.
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
            return

        self._processor.on_emit(log_record)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._processor.force_flush(timeout_millis))

    def shutdown(self) -> None:
        self._processor.shutdown()


__all__ = [
    "DEFAULT_CIRCUIT_BREAKER_SECONDS",
    "ENV_CIRCUIT_BREAKER_SECONDS",
    "ExportCircuitBreaker",
    "FailureReportingExporter",
    "TrackedBatchLogRecordProcessor",
    "TrackedLogRecordExporter",
    "resolve_circuit_breaker_seconds",
]
