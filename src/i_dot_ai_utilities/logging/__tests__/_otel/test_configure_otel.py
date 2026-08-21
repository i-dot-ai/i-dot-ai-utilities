# mypy: disable-error-code="no-untyped-def"
"""Additional tests for framework-neutral ``configure_otel``."""

from __future__ import annotations

import pytest

pytest.importorskip("opentelemetry")

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from i_dot_ai_utilities.logging._otel.setup import (
    _otlp_exporter_kwargs,
    _reset_for_tests,
    configure_otel,
)


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


class TestOtlpExporterKwargs:
    """All three signals must share endpoint and header handling.

    Regression guard: headers were previously applied to traces only, so an
    authenticated collector would accept spans but reject logs and metrics.
    """

    SIGNALS = ("/v1/traces", "/v1/metrics", "/v1/logs")

    def test_returns_none_without_endpoint(self, monkeypatch):
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
        for signal in self.SIGNALS:
            assert _otlp_exporter_kwargs(signal) is None

    def test_all_signals_receive_parsed_headers(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "Authorization=Bearer tkn, x-tenant=iai")
        for signal in self.SIGNALS:
            kwargs = _otlp_exporter_kwargs(signal)
            assert kwargs["endpoint"] == f"http://collector:4318{signal}"
            assert kwargs["headers"] == {
                "Authorization": "Bearer tkn",
                "x-tenant": "iai",
            }

    def test_headers_omitted_when_env_unset(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318")
        monkeypatch.delenv("OTEL_EXPORTER_OTLP_HEADERS", raising=False)
        for signal in self.SIGNALS:
            assert "headers" not in _otlp_exporter_kwargs(signal)

    def test_endpoint_suffix_is_not_duplicated(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/v1/traces")
        kwargs = _otlp_exporter_kwargs("/v1/traces")
        assert kwargs["endpoint"] == "http://collector:4318/v1/traces"

    def test_trailing_slash_endpoint_is_normalised(self, monkeypatch):
        monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://collector:4318/")
        kwargs = _otlp_exporter_kwargs("/v1/logs")
        assert kwargs["endpoint"] == "http://collector:4318/v1/logs"
