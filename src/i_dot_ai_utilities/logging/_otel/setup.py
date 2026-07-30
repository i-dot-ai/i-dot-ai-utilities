"""Framework-neutral OpenTelemetry bootstrap for i-dot-ai services.

``configure_otel`` is the single entrypoint for traces, metrics, and optional
log-bridge wiring. Framework helpers (``configure_otel_for_django``,
``configure_otel_for_fastapi``) call into it and enable auto-instrumentation.

Behaviour guarantees:

- Idempotent within a process (safe to call from AppConfig.ready / ASGI lifespan).
- X-Ray-compatible trace IDs via ``AwsXRayIdGenerator`` when available.
- Composite W3C + AWS X-Ray propagator (W3C wins on extract conflict).
- Resource attributes follow Semantic Conventions (``service.name``,
  ``deployment.environment.name``, optional ``service.version``).
- Legacy ``configure_otel_for_django`` remains a compatible wrapper.
"""

from __future__ import annotations

import contextlib
import os
from typing import TYPE_CHECKING, Any

from opentelemetry import metrics, trace
from opentelemetry.propagate import set_global_textmap
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from i_dot_ai_utilities.logging._otel.propagators import build_composite_propagator
from i_dot_ai_utilities.logging._otel.otlp_log_processor import (
    otel_otlp_log_emitter_processor,
)
from i_dot_ai_utilities.logging._otel.structlog_processor import (
    otel_trace_context_processor,
)

if TYPE_CHECKING:
    from opentelemetry.sdk.trace.export import SpanExporter

_django_instrumented: bool = False
_fastapi_instrumented: bool = False
_configured: bool = False


def _build_resource(service_name: str) -> Resource:
    """Compose a Semantic-Convention Resource for the process."""
    attrs: dict[str, Any] = {"service.name": service_name}
    version = os.environ.get("APP_VERSION") or os.environ.get("OTEL_SERVICE_VERSION")
    if version:
        attrs["service.version"] = version
    # Prefer the current SemConv name; fall back to legacy ENVIRONMENT.
    environment = (
        os.environ.get("OTEL_RESOURCE_ATTRIBUTES", "")
    )
    env_name = os.environ.get("DEPLOYMENT_ENVIRONMENT") or os.environ.get("ENVIRONMENT")
    # Parse deployment.environment.name from OTEL_RESOURCE_ATTRIBUTES if present.
    if "deployment.environment.name=" in environment:
        for part in environment.split(","):
            if part.startswith("deployment.environment.name="):
                env_name = part.split("=", 1)[1]
                break
    if env_name:
        attrs["deployment.environment.name"] = env_name
        # Do not dual-write legacy ``deployment.environment``: OpenSearch treats
        # dotted keys as nested paths, so emitting both that key and
        # ``deployment.environment.name`` causes permanent mapping conflicts.
    namespace = os.environ.get("SERVICE_NAMESPACE")
    if not namespace and "service.namespace=" in environment:
        for part in environment.split(","):
            if part.startswith("service.namespace="):
                namespace = part.split("=", 1)[1]
                break
    if namespace:
        attrs["service.namespace"] = namespace
    return Resource.create(attrs)


def _build_id_generator() -> Any | None:
    """Return an X-Ray-compatible IdGenerator when the AWS extension is installed."""
    with contextlib.suppress(ImportError):
        from opentelemetry.sdk.extension.aws.trace import AwsXRayIdGenerator

        return AwsXRayIdGenerator()
    return None


def _default_otlp_span_exporter() -> SpanExporter | None:
    """Build an OTLP HTTP span exporter from env, or None if unset."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return None
    with contextlib.suppress(ImportError):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        traces_endpoint = endpoint.rstrip("/")
        if not traces_endpoint.endswith("/v1/traces"):
            traces_endpoint = f"{traces_endpoint}/v1/traces"
        headers = _otlp_headers_from_env()
        kwargs: dict[str, Any] = {"endpoint": traces_endpoint}
        if headers:
            kwargs["headers"] = headers
        return OTLPSpanExporter(**kwargs)
    return None


def _otlp_headers_from_env() -> dict[str, str] | None:
    """Parse OTEL_EXPORTER_OTLP_HEADERS (comma-separated key=value)."""
    raw = os.environ.get("OTEL_EXPORTER_OTLP_HEADERS")
    if not raw:
        return None
    headers: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        headers[key.strip()] = value.strip()
    return headers or None


def _require_otlp_endpoint_or_raise() -> None:
    """Fail visibly in AWS when OTLP endpoint is required but missing."""
    require = os.environ.get("OTEL_REQUIRE_ENDPOINT", "").lower() in {"1", "true", "yes"}
    if require and not os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        raise RuntimeError(
            "OTEL_REQUIRE_ENDPOINT is set but OTEL_EXPORTER_OTLP_ENDPOINT is missing. "
            "Refusing silent console/localhost export."
        )


def _configure_metrics(resource: Resource) -> None:
    """Install a MeterProvider with OTLP export when the exporter is available."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    with contextlib.suppress(Exception):
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

        metrics_endpoint = endpoint.rstrip("/")
        if not metrics_endpoint.endswith("/v1/metrics"):
            metrics_endpoint = f"{metrics_endpoint}/v1/metrics"
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=metrics_endpoint),
            export_interval_millis=10000,
        )
        provider = MeterProvider(resource=resource, metric_readers=[reader])
        metrics.set_meter_provider(provider)


def _configure_logs_bridge(resource: Resource) -> None:
    """Install LoggerProvider + OTLP exporter so structlog can emit OTLP logs."""
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        return
    try:
        import logging

        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        logs_endpoint = endpoint.rstrip("/")
        if not logs_endpoint.endswith("/v1/logs"):
            logs_endpoint = f"{logs_endpoint}/v1/logs"
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=logs_endpoint))
        )
        set_logger_provider(provider)
        # Stdlib bridge for code paths that use logging.getLogger().
        handler = LoggingHandler(level=logging.NOTSET, logger_provider=provider)
        logging.getLogger().addHandler(handler)
    except Exception as exc:  # noqa: BLE001 - bootstrap must not crash the app
        import sys

        print(f"WARNING: OTLP logs bridge not configured: {exc}", file=sys.stderr)


def _insert_trace_processor(processors: list[Any]) -> list[Any]:
    new_processors = list(processors)
    for proc in (otel_trace_context_processor, otel_otlp_log_emitter_processor):
        if proc not in new_processors:
            if not new_processors:
                new_processors.append(proc)
            else:
                new_processors.insert(len(new_processors) - 1, proc)
    return new_processors


def configure_otel(
    *,
    service_name: str,
    span_exporter: SpanExporter | None = None,
    structlog_processors: list[Any] | None = None,
    install_global_propagator: bool = True,
    tracer_provider: TracerProvider | None = None,
    enable_metrics: bool = True,
    enable_log_bridge: bool = True,
) -> TracerProvider:
    """Configure OpenTelemetry for any runtime.

    :param service_name: Bound to ``service.name`` on every signal.
    :param span_exporter: Defaults to OTLP from env, else ConsoleSpanExporter.
    :param structlog_processors: Optional list mutated to include the
        trace-context processor before the renderer.
    :param install_global_propagator: Install W3C+X-Ray composite propagator.
    :param tracer_provider: Optional pre-built provider (tests).
    :param enable_metrics: Install OTLP MeterProvider when env endpoint is set.
    :param enable_log_bridge: Attach OTel LoggingHandler for OTLP logs.
    :returns: The installed ``TracerProvider``.
    """
    global _configured  # noqa: PLW0603

    _require_otlp_endpoint_or_raise()
    resource = _build_resource(service_name)

    if tracer_provider is None:
        id_generator = _build_id_generator()
        kwargs: dict[str, Any] = {"resource": resource}
        if id_generator is not None:
            kwargs["id_generator"] = id_generator
        tracer_provider = TracerProvider(**kwargs)
        if span_exporter is not None:
            exporter = span_exporter
        else:
            exporter = _default_otlp_span_exporter()
            if exporter is None:
                if os.environ.get("OTEL_REQUIRE_ENDPOINT", "").lower() in {"1", "true", "yes"}:
                    raise RuntimeError("OTLP span exporter unavailable despite required endpoint")
                exporter = ConsoleSpanExporter()
        tracer_provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(tracer_provider)

    if install_global_propagator:
        set_global_textmap(build_composite_propagator())

    if enable_metrics and not _configured:
        _configure_metrics(resource)
    if enable_log_bridge and not _configured:
        _configure_logs_bridge(resource)

    if structlog_processors is not None:
        updated = _insert_trace_processor(structlog_processors)
        structlog_processors.clear()
        structlog_processors.extend(updated)

    _configured = True
    return tracer_provider


def configure_otel_for_django(
    *,
    service_name: str,
    span_exporter: SpanExporter | None = None,
    structlog_processors: list[Any] | None = None,
    install_global_propagator: bool = True,
    tracer_provider: TracerProvider | None = None,
) -> TracerProvider:
    """Configure OpenTelemetry tracing for a Django service (compatible wrapper)."""
    global _django_instrumented  # noqa: PLW0603

    provider = configure_otel(
        service_name=service_name,
        span_exporter=span_exporter,
        structlog_processors=structlog_processors,
        install_global_propagator=install_global_propagator,
        tracer_provider=tracer_provider,
        enable_metrics=True,
        enable_log_bridge=True,
    )

    if not _django_instrumented:
        from opentelemetry.instrumentation.django import DjangoInstrumentor

        DjangoInstrumentor().instrument(tracer_provider=provider)
        _django_instrumented = True

    return provider


def configure_otel_for_fastapi(
    *,
    service_name: str,
    app: Any | None = None,
    span_exporter: SpanExporter | None = None,
    structlog_processors: list[Any] | None = None,
    install_global_propagator: bool = True,
    tracer_provider: TracerProvider | None = None,
) -> TracerProvider:
    """Configure OpenTelemetry for a FastAPI/ASGI application."""
    global _fastapi_instrumented  # noqa: PLW0603

    provider = configure_otel(
        service_name=service_name,
        span_exporter=span_exporter,
        structlog_processors=structlog_processors,
        install_global_propagator=install_global_propagator,
        tracer_provider=tracer_provider,
        enable_metrics=True,
        enable_log_bridge=True,
    )

    if not _fastapi_instrumented:
        with contextlib.suppress(ImportError):
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            if app is not None:
                FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
            else:
                FastAPIInstrumentor().instrument(tracer_provider=provider)
            _fastapi_instrumented = True

    return provider


def insert_trace_processor(processors: list[Any]) -> list[Any]:
    """Return ``processors`` with the trace-context processor inserted."""
    return _insert_trace_processor(processors)


def ensure_structlog_otel_processors() -> None:
    """Re-insert OTel processors after StructuredLogger resets structlog config.

    Call once after constructing ``StructuredLogger`` so apps do not need to
    hand-roll processor list surgery.
    """
    with contextlib.suppress(Exception):
        import structlog

        current = list(structlog.get_config().get("processors", []))
        updated = _insert_trace_processor(current)
        structlog.configure(processors=updated)


def configure_otel_for_lambda(
    *,
    service_name: str,
    span_exporter: SpanExporter | None = None,
) -> TracerProvider:
    """Bootstrap OTel for a short-lived Lambda handler.

    Sets a process-wide provider. Call :func:`force_flush_otel` at the end of
    each invocation (or use the AWS Lambda instrumentation layer separately).
    """
    os.environ.setdefault("OTEL_REQUIRE_ENDPOINT", "1")
    return configure_otel(
        service_name=service_name,
        span_exporter=span_exporter,
        enable_metrics=True,
        enable_log_bridge=True,
    )


def force_flush_otel(timeout_millis: int = 5000) -> None:
    """Flush traces/metrics/logs providers — required for Lambda/Batch exit."""
    with contextlib.suppress(Exception):
        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush(timeout_millis)
    with contextlib.suppress(Exception):
        meter = metrics.get_meter_provider()
        if hasattr(meter, "force_flush"):
            meter.force_flush(timeout_millis)
    with contextlib.suppress(Exception):
        from opentelemetry._logs import get_logger_provider

        logs_provider = get_logger_provider()
        if hasattr(logs_provider, "force_flush"):
            logs_provider.force_flush(timeout_millis)


def _reset_for_tests() -> None:
    """Reset module-level instrumentation flags (tests only)."""
    global _django_instrumented, _fastapi_instrumented, _configured  # noqa: PLW0603
    with contextlib.suppress(Exception):
        from opentelemetry.instrumentation.django import DjangoInstrumentor

        DjangoInstrumentor().uninstrument()
    with contextlib.suppress(Exception):
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor().uninstrument()
    _django_instrumented = False
    _fastapi_instrumented = False
    _configured = False


__all__ = [
    "configure_otel",
    "configure_otel_for_django",
    "configure_otel_for_fastapi",
    "configure_otel_for_lambda",
    "ensure_structlog_otel_processors",
    "force_flush_otel",
    "insert_trace_processor",
]
