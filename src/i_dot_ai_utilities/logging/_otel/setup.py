"""One-call OTel setup for Django services using this library.

``configure_otel_for_django`` is the single entrypoint consumers call at
process startup (in ``wsgi.py`` / ``asgi.py`` or an ``AppConfig.ready()``).
It performs four jobs:

1. Builds a :class:`TracerProvider` carrying a minimal ``Resource``
   (``service.name``, plus ``service.version`` /
   ``deployment.environment`` if the relevant env vars are set).
2. Attaches a span processor. By default this is a ``BatchSpanProcessor``
   over a ``ConsoleSpanExporter`` so operators can see spans during local
   development; production consumers pass a real exporter (OTLP / vendor).
3. Registers a composite propagator (W3C → X-Ray) as the global textmap,
   unless the caller opts out with ``install_global_propagator=False``.
4. Activates Django auto-instrumentation via
   :class:`DjangoInstrumentor`, which is the mechanism that produces
   per-request spans carrying ``http.request.method`` / ``url.path`` /
   ``http.route`` / ``http.response.status_code`` etc. as span attributes.

The function is idempotent: repeated calls update the process's tracer
provider and re-instrument Django only on the first call.

Structlog integration is optional and explicit. If the caller passes
``structlog_processors=[...]``, the function returns a new list with the
:func:`otel_trace_context_processor` inserted just before the renderer
(the last element). Callers pass that list to their own
``structlog.configure(...)`` — this module does not call
``structlog.configure`` (the library's existing Art. 22 constraint).

Sentry wiring is out of scope for this module: add a span exporter that
ships OTLP to Sentry or any other backend via the ``span_exporter``
argument.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING, Any

from opentelemetry import trace
from opentelemetry.instrumentation.django import DjangoInstrumentor
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from i_dot_ai_utilities.logging._otel.propagators import build_composite_propagator
from i_dot_ai_utilities.logging._otel.structlog_processor import (
    otel_trace_context_processor,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter


# Idempotency guard for DjangoInstrumentor, which logs a warning on
# repeated ``instrument()`` calls in the same process. Tracked here rather
# than relying on the instrumentor's internal state so repeated test
# setups stay quiet.
_django_instrumented: bool = False


def _build_resource(service_name: str) -> Resource:
    """Compose a minimal Resource for the TracerProvider.

    Only attributes we can confidently derive at process boot are included.
    ``service.name`` is required; ``service.version`` and
    ``deployment.environment`` are conventional opt-ins picked up from env
    vars that most i.AI deployments already set.
    """
    attrs: dict[str, Any] = {"service.name": service_name}
    version = os.environ.get("APP_VERSION")
    if version:
        attrs["service.version"] = version
    environment = os.environ.get("ENVIRONMENT")
    if environment:
        attrs["deployment.environment"] = environment
    return Resource.create(attrs)


def _insert_trace_processor(processors: list[Any]) -> list[Any]:
    """Return ``processors`` with the trace-context processor inserted.

    The structlog renderer is conventionally the *last* element of the
    processor chain (``JSONRenderer`` or ``ConsoleRenderer``). The trace
    processor must run before the renderer but after any context-merging
    processors, so "just before the last element" is the right slot.

    Empty lists get the processor appended. Lists already containing the
    trace processor are returned unchanged (idempotent).
    """
    if otel_trace_context_processor in processors:
        return list(processors)

    new_processors = list(processors)
    if not new_processors:
        new_processors.append(otel_trace_context_processor)
        return new_processors
    # Insert just before the last element (the renderer).
    new_processors.insert(len(new_processors) - 1, otel_trace_context_processor)
    return new_processors


def configure_otel_for_django(
    *,
    service_name: str,
    span_exporter: SpanExporter | None = None,
    structlog_processors: list[Any] | None = None,
    install_global_propagator: bool = True,
    tracer_provider: TracerProvider | None = None,
) -> TracerProvider:
    """Configure OpenTelemetry tracing for a Django service.

    :param service_name: Value bound to ``service.name`` on every span.
        Required; should match the service identity used elsewhere in the
        deployment (Grafana dashboards, alerting).
    :param span_exporter: Span exporter to wrap in a ``BatchSpanProcessor``.
        Defaults to ``ConsoleSpanExporter`` for visible local development.
        In production, pass an OTLP exporter pointing at your collector.
    :param structlog_processors: If provided, a copy of the list with
        :func:`otel_trace_context_processor` inserted before the renderer
        is returned via the ``processors`` attribute of the returned
        provider's resource — see :func:`insert_trace_processor` below for
        callers who want to wire the processor into their own structlog
        chain without going through this function.
    :param install_global_propagator: If ``True`` (default), replaces the
        global textmap with a W3C + AWS X-Ray composite. Set to ``False``
        if the caller already manages its own propagator.
    :param tracer_provider: Optional pre-built provider. Useful in tests
        where an ``InMemorySpanExporter`` needs to be wired directly. When
        passed, ``span_exporter`` is ignored.
    :returns: The ``TracerProvider`` installed globally.
    """
    global _django_instrumented  # noqa: PLW0603

    if tracer_provider is None:
        tracer_provider = TracerProvider(resource=_build_resource(service_name))
        exporter = span_exporter if span_exporter is not None else ConsoleSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(tracer_provider)

    if install_global_propagator:
        set_global_textmap(build_composite_propagator())

    if not _django_instrumented:
        DjangoInstrumentor().instrument(tracer_provider=tracer_provider)
        _django_instrumented = True

    if structlog_processors is not None:
        # Mutate-in-place semantics are easier for the caller: they pass
        # their list, we reorder and append as needed.
        updated = _insert_trace_processor(structlog_processors)
        structlog_processors.clear()
        structlog_processors.extend(updated)

    return tracer_provider


def insert_trace_processor(processors: list[Any]) -> list[Any]:
    """Return ``processors`` with the trace-context processor inserted.

    Convenience wrapper for callers who want to extend their structlog
    processor chain without also running the tracer/provider setup
    performed by :func:`configure_otel_for_django`.
    """
    return _insert_trace_processor(processors)


def _reset_for_tests() -> None:
    """Reset the module-level instrumentation flag.

    Intended for test suites that want to re-run ``configure_otel_for_django``
    against a fresh :class:`InMemorySpanExporter`. Not part of the stable
    public API — underscore-prefixed for that reason.
    """
    global _django_instrumented  # noqa: PLW0603
    with contextlib.suppress(Exception):
        DjangoInstrumentor().uninstrument()
    _django_instrumented = False


__all__ = [
    "configure_otel_for_django",
    "insert_trace_processor",
]
