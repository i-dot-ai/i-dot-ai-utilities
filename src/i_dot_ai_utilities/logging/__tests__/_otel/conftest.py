# mypy: disable-error-code="no-untyped-def"
"""Shared fixtures for the `_otel` subpackage tests.

Provides:

- ``in_memory_exporter`` — fresh ``InMemorySpanExporter`` per test.
- ``tracer_provider_with_memory_exporter`` — :class:`TracerProvider`
  wired to the in-memory exporter and made global for the duration of
  the test. Uses a ``SimpleSpanProcessor`` so exported spans are
  readable synchronously after each test request.
- ``reset_otel_state`` (autouse) — restores global OTel state
  (propagator, tracer provider, DjangoInstrumentor activation) after
  every test so tests are independent.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry.propagate import get_global_textmap, set_global_textmap
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

if TYPE_CHECKING:
    from collections.abc import Generator


@pytest.fixture
def in_memory_exporter() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def tracer_provider_with_memory_exporter(
    in_memory_exporter: InMemorySpanExporter,
) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({"service.name": "test-service"}))
    # SimpleSpanProcessor exports synchronously on span-end; tests can
    # read spans immediately after the request has finished.
    provider.add_span_processor(SimpleSpanProcessor(in_memory_exporter))
    return provider


@pytest.fixture(autouse=True)
def reset_otel_state() -> Generator[None, None, None]:
    """Best-effort OTel state restoration between tests.

    Restores the global textmap after each test so propagator-
    installation tests don't bleed into sibling tests.

    We deliberately do NOT call ``DjangoInstrumentor().uninstrument()``
    here. The ``middleware/test_django_otel.py`` module calls
    ``configure_otel_for_django`` at module-load time to wire a shared
    ``InMemorySpanExporter``; an aggressive teardown in this conftest
    would undo that wiring when the module test order puts ``_otel``
    tests first. Tests that genuinely need to reset the instrumentor
    should do so explicitly.
    """
    prev_textmap = get_global_textmap()
    try:
        yield
    finally:
        set_global_textmap(prev_textmap)
