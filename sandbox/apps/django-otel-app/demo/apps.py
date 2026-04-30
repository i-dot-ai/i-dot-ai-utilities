"""AppConfig.ready() hooks — configure OpenTelemetry + wire the structlog
trace-context processor into the i-dot-ai-utilities StructuredLogger.

The StructuredLogger resets structlog's global processor chain inside its
`__init__`. That means we can't just pass `structlog_processors=[...]` to
`configure_otel_for_django` before the logger is constructed and expect
the processor to stick — the logger would overwrite it.

Workaround (documented in the sandbox README): we run OTel setup first,
then post-hoc insert `otel_trace_context_processor` just before structlog's
renderer. Idempotent: if the processor is already present (e.g. if ready()
fires twice) we skip.
"""

from __future__ import annotations

import structlog
from django.apps import AppConfig
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from i_dot_ai_utilities.logging._otel import (
    configure_otel_for_django,
    otel_trace_context_processor,
)


class DemoConfig(AppConfig):
    name = "demo"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self) -> None:
        # Set up the global TracerProvider, install the composite propagator,
        # and auto-instrument Django so per-request server spans carry the
        # http.request.method / url.path / http.route / etc. attributes.
        configure_otel_for_django(
            service_name="django-otel-demo",
            # OTLP over HTTP to the LGTM all-in-one image's Tempo ingest.
            span_exporter=OTLPSpanExporter(
                endpoint="http://lgtm:4318/v1/traces",
            ),
        )

        # StructuredLogger's __init__ has already run (it lives on settings.LOGGER),
        # which means structlog is globally configured with its processor chain
        # but without our trace-context processor. Inject it here.
        current = list(structlog.get_config().get("processors", []))
        if otel_trace_context_processor not in current:
            # The renderer (JSONRenderer / ConsoleRenderer) is the last element
            # of the chain by convention. Insert just before it.
            if current:
                current.insert(len(current) - 1, otel_trace_context_processor)
            else:
                current = [otel_trace_context_processor]
            structlog.configure(processors=current)
