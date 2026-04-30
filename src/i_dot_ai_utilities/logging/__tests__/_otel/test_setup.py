# mypy: disable-error-code="no-untyped-def"
"""Tests for ``_otel.setup.configure_otel_for_django`` and helpers.

Important constraint: OpenTelemetry's ``trace.set_tracer_provider`` logs
``Overriding of current TracerProvider is not allowed`` on the second
call in the same process. These tests avoid that by doing provider-level
assertions via the returned object rather than via
``trace.get_tracer_provider()``.

Django is required because ``setup.configure_otel_for_django`` calls
``DjangoInstrumentor().instrument()``; we configure Django settings
inside the conftest/fixture path rather than here so tests can still be
collected when Django is absent.
"""

from __future__ import annotations

import pytest

django = pytest.importorskip("django")
pytest.importorskip("opentelemetry")

from django.conf import settings as django_settings  # type: ignore[import-untyped]  # noqa: E402

if not django_settings.configured:
    django_settings.configure(
        DEBUG=False,
        ALLOWED_HOSTS=["*"],
        DATABASES={},
        INSTALLED_APPS=[],
        ROOT_URLCONF=__name__,
        MIDDLEWARE=[],
        SECRET_KEY="test-secret-not-for-production",
        USE_TZ=True,
    )
    import django as _django  # type: ignore[import-untyped]

    _django.setup()

from opentelemetry.propagate import get_global_textmap  # noqa: E402
from opentelemetry.propagators.composite import CompositePropagator  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from i_dot_ai_utilities.logging._otel.setup import (  # noqa: E402
    configure_otel_for_django,
    insert_trace_processor,
)
from i_dot_ai_utilities.logging._otel.structlog_processor import (  # noqa: E402
    otel_trace_context_processor,
)


class TestInsertTraceProcessor:
    def test_empty_list_gets_processor_appended(self):
        result = insert_trace_processor([])
        assert result == [otel_trace_context_processor]

    def test_processor_inserted_before_renderer(self):
        # Simulate a structlog chain: merge_contextvars, add_log_level, JSONRenderer.
        def merge_contextvars(*_args, **_kwargs):
            return {}

        def add_log_level(*_args, **_kwargs):
            return {}

        def json_renderer(*_args, **_kwargs):
            return "{}"

        chain = [merge_contextvars, add_log_level, json_renderer]
        result = insert_trace_processor(chain)

        assert result[0] is merge_contextvars
        assert result[1] is add_log_level
        assert result[2] is otel_trace_context_processor
        assert result[3] is json_renderer

    def test_idempotent_when_processor_already_present(self):
        def _placeholder(*_args, **_kwargs):
            return "x"

        chain: list = [otel_trace_context_processor, _placeholder]
        result = insert_trace_processor(chain)
        # Exactly one occurrence of the processor.
        assert result.count(otel_trace_context_processor) == 1

    def test_does_not_mutate_input_list(self):
        def _placeholder(*_args, **_kwargs):
            return "x"

        chain: list = [_placeholder]
        original_len = len(chain)
        insert_trace_processor(chain)
        assert len(chain) == original_len


class TestConfigureOTelForDjango:
    def test_passing_tracer_provider_routes_spans_to_its_exporter(
        self, tracer_provider_with_memory_exporter, in_memory_exporter
    ):
        configure_otel_for_django(
            service_name="svc",
            tracer_provider=tracer_provider_with_memory_exporter,
            install_global_propagator=False,
        )
        tracer = tracer_provider_with_memory_exporter.get_tracer(__name__)
        with tracer.start_as_current_span("work"):
            pass

        spans = in_memory_exporter.get_finished_spans()
        assert len(spans) == 1
        assert spans[0].name == "work"

    def test_install_global_propagator_true_replaces_textmap(
        self, tracer_provider_with_memory_exporter
    ):
        configure_otel_for_django(
            service_name="svc",
            tracer_provider=tracer_provider_with_memory_exporter,
            install_global_propagator=True,
        )
        textmap = get_global_textmap()
        assert isinstance(textmap, CompositePropagator)

    def test_install_global_propagator_false_leaves_textmap_alone(
        self, tracer_provider_with_memory_exporter
    ):
        before = get_global_textmap()
        configure_otel_for_django(
            service_name="svc",
            tracer_provider=tracer_provider_with_memory_exporter,
            install_global_propagator=False,
        )
        after = get_global_textmap()
        assert after is before

    def test_structlog_processors_list_receives_trace_processor(
        self, tracer_provider_with_memory_exporter
    ):
        def _placeholder(*_args, **_kwargs):
            return "x"

        processors: list = [_placeholder]
        configure_otel_for_django(
            service_name="svc",
            tracer_provider=tracer_provider_with_memory_exporter,
            install_global_propagator=False,
            structlog_processors=processors,
        )
        assert otel_trace_context_processor in processors

    def test_structlog_processors_none_is_fine(
        self, tracer_provider_with_memory_exporter
    ):
        # Should not raise.
        provider = configure_otel_for_django(
            service_name="svc",
            tracer_provider=tracer_provider_with_memory_exporter,
            install_global_propagator=False,
            structlog_processors=None,
        )
        assert provider is tracer_provider_with_memory_exporter

    def test_default_exporter_path_does_not_raise(self):
        """Smoke test: when no provider is passed, setup builds a default
        provider and returns it without raising. We inject an
        ``InMemorySpanExporter`` to avoid pytest-vs-ConsoleSpanExporter
        shutdown races leaving noise in the teardown log."""
        provider = configure_otel_for_django(
            service_name="svc",
            span_exporter=InMemorySpanExporter(),
            install_global_propagator=False,
        )
        assert isinstance(provider, TracerProvider)
        # Tracer usable.
        tracer = provider.get_tracer(__name__)
        with tracer.start_as_current_span("probe"):
            pass


class TestBuildResourceEnvVars:
    """``_build_resource`` reads ``APP_VERSION`` and ``ENVIRONMENT`` from
    the process environment and binds them as OTel resource attributes.

    These branches are easy to break and drive production observability
    — ``service.version`` gates vendor-side "version compare" views and
    ``deployment.environment`` is the primary filter across every
    Grafana / Tempo dashboard. We pin the behaviour directly here rather
    than only through integration coverage.
    """

    def _resource_attrs(self, provider: TracerProvider) -> dict:
        """Return the provider's Resource attributes as a plain dict.

        The Resource API exposes a read-only mapping; converting makes
        the ``in`` / ``not in`` assertions below concise.
        """
        return dict(provider.resource.attributes)

    def test_app_version_env_var_becomes_service_version(self, monkeypatch):
        monkeypatch.setenv("APP_VERSION", "1.2.3")
        monkeypatch.delenv("ENVIRONMENT", raising=False)

        provider = configure_otel_for_django(
            service_name="svc",
            span_exporter=InMemorySpanExporter(),
            install_global_propagator=False,
        )
        attrs = self._resource_attrs(provider)
        assert attrs.get("service.version") == "1.2.3"
        assert attrs.get("service.name") == "svc"

    def test_environment_env_var_becomes_deployment_environment(self, monkeypatch):
        monkeypatch.delenv("APP_VERSION", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "prod")

        provider = configure_otel_for_django(
            service_name="svc",
            span_exporter=InMemorySpanExporter(),
            install_global_propagator=False,
        )
        attrs = self._resource_attrs(provider)
        assert attrs.get("deployment.environment") == "prod"

    def test_both_env_vars_bound_when_set(self, monkeypatch):
        monkeypatch.setenv("APP_VERSION", "4.5.6")
        monkeypatch.setenv("ENVIRONMENT", "staging")

        provider = configure_otel_for_django(
            service_name="svc",
            span_exporter=InMemorySpanExporter(),
            install_global_propagator=False,
        )
        attrs = self._resource_attrs(provider)
        assert attrs.get("service.version") == "4.5.6"
        assert attrs.get("deployment.environment") == "staging"

    def test_missing_env_vars_omit_attributes_rather_than_binding_empty(
        self, monkeypatch
    ):
        """Absent env vars MUST NOT be bound as empty-string attributes —
        downstream queries doing ``deployment.environment != ""`` would
        produce false positives. ``_build_resource`` explicitly checks
        truthiness before binding.
        """
        monkeypatch.delenv("APP_VERSION", raising=False)
        monkeypatch.delenv("ENVIRONMENT", raising=False)

        provider = configure_otel_for_django(
            service_name="svc",
            span_exporter=InMemorySpanExporter(),
            install_global_propagator=False,
        )
        attrs = self._resource_attrs(provider)
        assert "service.version" not in attrs
        assert "deployment.environment" not in attrs

    def test_empty_string_env_vars_are_treated_as_unset(self, monkeypatch):
        """``os.environ["APP_VERSION"] = ""`` should NOT produce
        ``service.version=""``. The helper uses ``if version:`` which
        treats empty strings as falsy, same for ``ENVIRONMENT``.
        """
        monkeypatch.setenv("APP_VERSION", "")
        monkeypatch.setenv("ENVIRONMENT", "")

        provider = configure_otel_for_django(
            service_name="svc",
            span_exporter=InMemorySpanExporter(),
            install_global_propagator=False,
        )
        attrs = self._resource_attrs(provider)
        assert "service.version" not in attrs
        assert "deployment.environment" not in attrs
