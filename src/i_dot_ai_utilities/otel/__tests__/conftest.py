# ruff: noqa: SLF001 - these tests deliberately inspect private state and SDK internals.
"""Shared fixtures and test doubles for ``i_dot_ai_utilities.otel`` tests.

Important SDK constraint: OpenTelemetry's global ``set_tracer_provider`` and
``set_logger_provider`` hooks use ``Once`` guards that cannot be reset within a
process. Unit tests therefore construct local ``LoggerProvider()`` and
``TracerProvider()`` instances in-process, while any test that calls
``configure_otel()`` runs in a fresh Python subprocess.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

import pytest
from opentelemetry.sdk._logs.export import LogRecordExporter, LogRecordExportResult

from i_dot_ai_utilities.otel import _attributes, _configuration
from i_dot_ai_utilities.otel._instrumentation import uninstrument_installed_libraries
from i_dot_ai_utilities.otel._lambda import reset_inflight_flush

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

    from opentelemetry.sdk._logs import ReadableLogRecord


class FakeClock:
    """Deterministic monotonic clock for circuit-breaker unit tests."""

    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class ControllableLogExporter(LogRecordExporter):
    """In-memory ``LogRecordExporter`` whose return status and exceptions can be scripted per test."""

    def __init__(
        self,
        *,
        result: LogRecordExportResult = LogRecordExportResult.SUCCESS,
        raise_on_export: bool = False,
        flush_result: bool = True,
        raise_on_flush: bool = False,
    ) -> None:
        self.result = result
        self.raise_on_export = raise_on_export
        self.flush_result = flush_result
        self.raise_on_flush = raise_on_flush
        self.batches: list[list[ReadableLogRecord]] = []
        self.shutdown_called = False

    def export(self, batch: Sequence[ReadableLogRecord]) -> LogRecordExportResult:
        self.batches.append(list(batch))
        if self.raise_on_export:
            msg = "network down"
            raise RuntimeError(msg)
        return self.result

    def force_flush(self, _timeout_millis: int = 30000) -> bool:
        if self.raise_on_flush:
            msg = "flush failed"
            raise RuntimeError(msg)
        return self.flush_result

    def shutdown(self) -> None:
        self.shutdown_called = True


def reset_otel_state() -> None:
    """Reset module-level state, root handlers, and instrumentors between in-process unit tests.

    Does not reset the OpenTelemetry SDK's global provider ``Once`` locks; run
    ``configure_otel()`` calls in a subprocess instead of in-process.
    """
    state = _configuration._state
    root_logger = logging.getLogger()
    if state.root_logging_handler is not None and state.root_logging_handler in root_logger.handlers:
        root_logger.removeHandler(state.root_logging_handler)
    if state.logger_provider is not None:
        with contextlib.suppress(Exception):
            state.logger_provider.shutdown()
    reset_inflight_flush()
    uninstrument_installed_libraries()
    _attributes._warned_keys.clear()
    _configuration._state = _configuration._State()


@pytest.fixture(autouse=True)
def _clean_otel_state() -> Iterator[None]:
    reset_otel_state()
    yield
    reset_otel_state()
