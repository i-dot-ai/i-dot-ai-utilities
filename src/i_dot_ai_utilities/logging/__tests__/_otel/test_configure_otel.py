"""Additional tests for framework-neutral ``configure_otel``."""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from i_dot_ai_utilities.logging._otel.setup import configure_otel, _reset_for_tests


@pytest.fixture(autouse=True)
def _reset_flags():
    _reset_for_tests()
    yield
    _reset_for_tests()


class TestConfigureOTel:
    def test_returns_tracer_provider(self):
        provider = configure_otel(
            service_name="neutral-svc",
            span_exporter=InMemorySpanExporter(),
            install_global_propagator=False,
            enable_metrics=False,
            enable_log_bridge=False,
        )
        assert isinstance(provider, TracerProvider)

    def test_resource_includes_semconv_environment_name(self, monkeypatch):
        monkeypatch.setenv("ENVIRONMENT", "sandbox")
        provider = configure_otel(
            service_name="neutral-svc",
            span_exporter=InMemorySpanExporter(),
            install_global_propagator=False,
            enable_metrics=False,
            enable_log_bridge=False,
        )
        attrs = dict(provider.resource.attributes)
        assert attrs.get("deployment.environment.name") == "sandbox"
        assert "deployment.environment" not in attrs

    def test_require_endpoint_raises_when_missing(self, monkeypatch):
        monkeypatch.setenv("OTEL_REQUIRE_ENDPOINT", "1")
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        with pytest.raises(RuntimeError, match="OTEL_REQUIRE_ENDPOINT"):
            configure_otel(
                service_name="aws-svc",
                install_global_propagator=False,
                enable_metrics=False,
                enable_log_bridge=False,
            )
