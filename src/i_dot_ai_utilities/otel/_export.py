"""Tracked OTLP log export and outage circuit breaker for the native OTel track."""

from __future__ import annotations

import os
import threading
import time
from typing import TYPE_CHECKING, Final

from opentelemetry.sdk._logs import LogRecordProcessor, ReadWriteLogRecord
from opentelemetry.sdk._logs.export import (
    BatchLogRecordProcessor,
    LogRecordExporter,
    LogRecordExportResult,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from opentelemetry.sdk._logs import ReadableLogRecord

ENV_CIRCUIT_BREAKER_SECONDS: Final[str] = "I_DOT_AI_OTEL_LAMBDA_CIRCUIT_BREAKER_SECONDS"
DEFAULT_CIRCUIT_BREAKER_SECONDS: Final[float] = 30.0


def resolve_circuit_breaker_seconds() -> float:
    """Return the open-circuit duration in seconds from the environment."""
    raw = os.environ.get(ENV_CIRCUIT_BREAKER_SECONDS)
    if not raw:
        return DEFAULT_CIRCUIT_BREAKER_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CIRCUIT_BREAKER_SECONDS
    return value if value >= 0.0 else DEFAULT_CIRCUIT_BREAKER_SECONDS


class ExportCircuitBreaker:
    """Process-local monotonic circuit breaker for synchronous Lambda flushes."""

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
        with self._lock:
            if self._open_until is None:
                return False
            if self._clock() < self._open_until:
                return True
            self._open_until = None
            return False

    def record_success(self) -> None:
        with self._lock:
            self._open_until = None

    def record_failure(self) -> None:
        with self._lock:
            self._open_until = self._clock() + self._open_seconds


class TrackedLogRecordExporter(LogRecordExporter):
    """Wraps a ``LogRecordExporter`` and records export outcomes on the breaker."""

    def __init__(
        self,
        delegate: LogRecordExporter,
        breaker: ExportCircuitBreaker,
    ) -> None:
        self._delegate = delegate
        self._breaker = breaker
        self._lock = threading.Lock()
        self._failed_since_reset = False

    def reset_window(self) -> None:
        with self._lock:
            self._failed_since_reset = False

    def consume_failure(self) -> bool:
        with self._lock:
            failed = self._failed_since_reset
            self._failed_since_reset = False
            return failed

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        # Once one batch fails, fail the rest of this flush immediately so an
        # outage costs one exporter timeout, not one per queued batch.
        if self._breaker.is_open():
            return LogRecordExportResult.FAILURE

        try:
            result = self._delegate.export(batch)
        except Exception:  # noqa: BLE001 - exporter contract catches all export errors
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
    """Wraps ``BatchLogRecordProcessor`` and keeps records queued while the breaker is open."""

    def __init__(self, exporter: TrackedLogRecordExporter) -> None:
        self._exporter = exporter
        self._processor = BatchLogRecordProcessor(exporter)
        batch_processor = getattr(self._processor, "_batch_processor", None)
        self._queue = getattr(batch_processor, "_queue", None)

    @property
    def exporter(self) -> TrackedLogRecordExporter:
        return self._exporter

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        if self._exporter._breaker.is_open() and self._queue is not None:  # noqa: SLF001
            # Do not wake the background export worker while the breaker is open;
            # the bounded deque drops the oldest record once it is full.
            from copy import copy  # noqa: PLC0415

            from opentelemetry.context import Context  # noqa: PLC0415
            from opentelemetry.sdk._logs import ReadableLogRecord  # noqa: PLC0415
            from opentelemetry.sdk.resources import Resource  # noqa: PLC0415

            resource = log_record.resource if log_record.resource is not None else Resource.create({})
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
    "TrackedBatchLogRecordProcessor",
    "TrackedLogRecordExporter",
    "resolve_circuit_breaker_seconds",
]
