# mypy: disable-error-code="no-untyped-def"
"""Integration tests for ``StructuredLoggingMiddlewareOTel``.

Mirrors ``test_middleware_django.py`` in style: a ``RecordingLogger``
double captures everything the middleware does, so assertions are made
on structured fields without needing structlog configuration.

Span-level assertions use an ``InMemorySpanExporter`` wired via
:func:`configure_otel_for_django` in the module-level setup. Because
OpenTelemetry refuses to replace a global tracer provider once set, the
provider is configured exactly once per process.
"""

from __future__ import annotations

import re
from typing import Any

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

import structlog  # noqa: E402
from django.core.exceptions import (  # type: ignore[import-untyped]  # noqa: E402
    ImproperlyConfigured,
    MiddlewareNotUsed,
)
from django.http import Http404, HttpResponse  # type: ignore[import-untyped]  # noqa: E402
from django.test import RequestFactory  # type: ignore[import-untyped]  # noqa: E402
from opentelemetry.sdk.resources import Resource  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (  # noqa: E402
    InMemorySpanExporter,
)

from i_dot_ai_utilities.logging._otel.setup import (  # noqa: E402
    configure_otel_for_django,
)
from i_dot_ai_utilities.logging.middleware.django_otel import (  # noqa: E402
    StructuredLoggingMiddlewareOTel,
)
from i_dot_ai_utilities.logging.structured_logger import (  # noqa: E402
    _request_scope_owner,
)

urlpatterns: list = []

UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")


# ---------------------------------------------------------------------------
# Module-level OTel setup.
#
# OpenTelemetry refuses to replace a TracerProvider once ``set_tracer_provider``
# has been called, so we configure exactly once for the whole test module and
# share the in-memory exporter via a module-global.
# ---------------------------------------------------------------------------


_SHARED_EXPORTER = InMemorySpanExporter()
_SHARED_PROVIDER = TracerProvider(resource=Resource.create({"service.name": "otel-mw-tests"}))
_SHARED_PROVIDER.add_span_processor(SimpleSpanProcessor(_SHARED_EXPORTER))

configure_otel_for_django(
    service_name="otel-mw-tests",
    tracer_provider=_SHARED_PROVIDER,
    install_global_propagator=True,
)


@pytest.fixture(autouse=True)
def _clear_exporter():
    _SHARED_EXPORTER.clear()
    yield
    _SHARED_EXPORTER.clear()


@pytest.fixture(autouse=True)
def _clear_structlog_contextvars():
    """Art. 57: ensure no structlog context bleeds between tests."""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Test double: records every call made by the middleware.
# ---------------------------------------------------------------------------


class RecordingLogger:
    """Structured-logger double matching ``_StructuredLoggerLike``."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {}
        self.refresh_calls: list[tuple[list[Any] | None, str]] = []

    def info(self, message_template: str, **kwargs: Any) -> None:
        self._record("info", message_template, kwargs)

    def warning(self, message_template: str, **kwargs: Any) -> None:
        self._record("warning", message_template, kwargs)

    def error(self, message_template: str, **kwargs: Any) -> None:
        self._record("error", message_template, kwargs)

    def exception(self, message_template: str, **kwargs: Any) -> None:
        self._record("error", message_template, kwargs, exception=True)

    def set_context_field(self, key: str, value: Any) -> None:
        self.context[key] = value

    def refresh_context(
        self,
        context_enrichers: list[Any] | None = None,
        scope: str = "manual",
    ) -> None:
        self.refresh_calls.append((context_enrichers, scope))
        # Mirror the real refresh: clear prior context.
        self.context = {}

    def _record(
        self,
        level: str,
        event: str,
        kwargs: dict[str, Any],
        *,
        exception: bool = False,
    ) -> None:
        record = {
            "log_level": level,
            "event": event,
            "exception_captured": exception,
            **self.context,
            **kwargs,
        }
        self.events.append(record)

    def events_for(self, event: str) -> list[dict[str, Any]]:
        return [e for e in self.events if e["event"] == event]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def rf() -> RequestFactory:
    return RequestFactory()


@pytest.fixture
def logger() -> RecordingLogger:
    return RecordingLogger()


@pytest.fixture
def settings_sandbox():
    """Restore any attribute changes on ``django.conf.settings``."""
    snapshot: dict[str, Any] = {}
    marker = object()

    def apply(**kwargs: Any) -> None:
        for k, v in kwargs.items():
            snapshot[k] = getattr(django_settings, k, marker)
            setattr(django_settings, k, v)

    yield apply

    for k, v in snapshot.items():
        if v is marker:
            if hasattr(django_settings, k):
                delattr(django_settings, k)
        else:
            setattr(django_settings, k, v)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_200_emits_started_and_completed(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        sentinel = HttpResponse(status=200)

        def get_response(_request):
            return sentinel

        mw = StructuredLoggingMiddlewareOTel(get_response)
        result = mw(rf.get("/users"))
        assert result is sentinel

        started = logger.events_for("request_started")
        completed = logger.events_for("request_completed")
        failed = logger.events_for("request_failed")
        assert len(started) == 1
        assert len(completed) == 1
        assert len(failed) == 0

        assert started[0]["log_level"] == "info"
        assert completed[0]["log_level"] == "info"
        assert completed[0]["http.response.status_code"] == 200
        assert isinstance(completed[0]["duration_ms"], int)
        assert completed[0]["duration_ms"] >= 0
        # request_id always present; UUID4 hex when no inbound header.
        assert UUID_HEX_RE.match(completed[0]["request_id"])

    def test_refresh_context_called_with_no_enrichers_and_request_scope(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/anything"))

        # Exactly one refresh_context call, with no enrichers and scope="request".
        assert len(logger.refresh_calls) == 1
        enrichers, scope = logger.refresh_calls[0]
        assert enrichers is None
        assert scope == "request"

    def test_log_line_does_not_contain_http_fields(self, rf, logger, settings_sandbox):
        """HTTP context fields belong on the OTel span, not on log records."""
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/things?foo=bar", HTTP_USER_AGENT="curl/8.0"))

        completed = logger.events_for("request_completed")[0]
        # None of these fields should be on the log record.
        forbidden = (
            "http.request.method",
            "url.scheme",
            "url.path",
            "url.query",
            "server.address",
            "user_agent.original",
            "client.address",
            "http.route",
            "django.url_name",
            "user.id",
        )
        for field in forbidden:
            assert field not in completed, f"{field!r} leaked onto log record"

    def test_span_is_recorded_by_django_instrumentor(self, logger, settings_sandbox):
        """DjangoInstrumentor creates a server span with HTTP attributes.

        Uses Django's full test client so the request flows through
        ``BaseHandler`` — which is what the instrumentor patches via its
        own middleware entry added to ``settings.MIDDLEWARE``.
        ``RequestFactory`` alone bypasses that layer.
        """
        from django.http import HttpResponse as _HttpResponse  # noqa: PLC0415
        from django.test import Client  # noqa: PLC0415
        from django.urls import path  # noqa: PLC0415

        def _ok(_request):
            return _HttpResponse(status=200)

        # Preserve the OTel instrumentation middleware added by
        # DjangoInstrumentor() — it ships as the first entry of
        # settings.MIDDLEWARE and we must keep it alongside our own.
        otel_mw = [
            m for m in django_settings.MIDDLEWARE if "opentelemetry" in m.lower()
        ]
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            ROOT_URLCONF=__name__,
            MIDDLEWARE=[
                *otel_mw,
                "i_dot_ai_utilities.logging.middleware.django_otel.StructuredLoggingMiddlewareOTel",
            ],
        )
        global urlpatterns  # noqa: PLW0603 - ROOT_URLCONF points at this module so tests must swap its urlpatterns in and out
        prev = urlpatterns
        urlpatterns = [path("users", _ok)]
        try:
            client = Client()
            response = client.get("/users")
        finally:
            urlpatterns = prev

        assert response.status_code == 200
        spans = _SHARED_EXPORTER.get_finished_spans()
        assert spans, "DjangoInstrumentor did not emit a span"
        attrs_by_span = [dict(s.attributes or {}) for s in spans]
        has_http_method = any(
            "http.request.method" in a or "http.method" in a for a in attrs_by_span
        )
        assert has_http_method, f"No span carried an HTTP method attribute. Attrs: {attrs_by_span}"


# ---------------------------------------------------------------------------
# Status-driven levels
# ---------------------------------------------------------------------------


class TestStatusLevels:
    @pytest.mark.parametrize(
        ("status", "expected_level"),
        [(200, "info"), (301, "info"), (404, "warning"), (500, "error")],
    )
    def test_level_reflects_status(self, status, expected_level, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=status))
        mw(rf.get("/p"))

        completed = logger.events_for("request_completed")
        assert len(completed) == 1
        assert completed[0]["log_level"] == expected_level
        assert completed[0]["http.response.status_code"] == status


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class TestExceptions:
    def test_view_exception_emits_failed_at_error_and_reraises(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def boom(_request):
            msg = "boom"
            raise RuntimeError(msg)

        mw = StructuredLoggingMiddlewareOTel(boom)

        with pytest.raises(RuntimeError, match="boom"):
            mw(rf.get("/danger"))

        failed = logger.events_for("request_failed")
        assert len(failed) == 1
        assert failed[0]["log_level"] == "error"
        assert failed[0]["exception_captured"] is True
        assert failed[0]["exception.type"] == "RuntimeError"
        assert failed[0]["http.response.status_code"] == 500
        assert isinstance(failed[0]["duration_ms"], int)
        assert failed[0]["duration_ms"] >= 0

        # No completed event on failure.
        assert logger.events_for("request_completed") == []

    def test_process_exception_hook_is_noop(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        events_before = len(logger.events)
        result = mw.process_exception(rf.get("/"), RuntimeError("nope"))
        assert result is None
        # The no-op hook must not produce any new log events.
        assert len(logger.events) == events_before


# ---------------------------------------------------------------------------
# Exclusions
# ---------------------------------------------------------------------------


class TestExclusions:
    def test_excluded_prefix_skips_logging_but_calls_view(self, rf, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_EXCLUDED_PREFIXES=("/healthz",),
        )
        called = {"flag": False}

        def view(_request):
            called["flag"] = True
            return HttpResponse(status=200)

        mw = StructuredLoggingMiddlewareOTel(view)
        mw(rf.get("/healthz/ready"))

        assert called["flag"] is True
        # No lifecycle events.
        assert logger.events_for("request_started") == []
        assert logger.events_for("request_completed") == []
        assert logger.events_for("request_failed") == []

    def test_excluded_regex_skips_logging(self, rf, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_EXCLUDED_REGEXES=(r"^/internal/",),
        )
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/internal/metrics"))

        assert logger.events_for("request_started") == []


# ---------------------------------------------------------------------------
# Header allowlist
# ---------------------------------------------------------------------------


class TestHeaderAllowlist:
    def test_allowlisted_header_is_captured(self, rf, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=("X-Tenant-ID",),
        )
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/x", HTTP_X_TENANT_ID="acme"))

        completed = logger.events_for("request_completed")[0]
        assert completed["http.request.header.x_tenant_id"] == "acme"

    def test_non_allowlisted_header_not_captured(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/x", HTTP_X_SECRET="nope"))

        completed = logger.events_for("request_completed")[0]
        assert not any(k.startswith("http.request.header.") for k in completed)

    def test_forbidden_header_refused_even_if_allowlisted(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=("Authorization", "X-Tenant-ID"),
        )
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/x", HTTP_AUTHORIZATION="Bearer s3cret", HTTP_X_TENANT_ID="acme"))

        completed = logger.events_for("request_completed")[0]
        # Authorization must not appear under any spelling.
        assert "http.request.header.authorization" not in completed
        # The legitimate header still flows through.
        assert completed["http.request.header.x_tenant_id"] == "acme"


# ---------------------------------------------------------------------------
# Request ID handling
# ---------------------------------------------------------------------------


class TestRequestId:
    """Constitution Art. 30 + 32: ``request_id`` is ALWAYS a fresh per-hop
    UUID4, distinct from any inbound correlation value. Any valid inbound
    ``X-Request-ID`` is preserved verbatim as ``upstream_request_id``.
    Charset-invalid or absent inbound values produce no
    ``upstream_request_id`` key at all (not an empty string).
    """

    def test_request_id_is_always_fresh_uuid_even_with_inbound_header(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/", HTTP_X_REQUEST_ID="external-id-123"))

        completed = logger.events_for("request_completed")[0]
        # Fresh UUID4, NOT the inbound value.
        assert UUID_HEX_RE.match(completed["request_id"])
        assert completed["request_id"] != "external-id-123"
        # Inbound preserved in its own field.
        assert completed["upstream_request_id"] == "external-id-123"

    def test_fresh_uuid_when_no_inbound_header(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/"))

        completed = logger.events_for("request_completed")[0]
        assert UUID_HEX_RE.match(completed["request_id"])
        # No inbound header → no upstream_request_id key at all.
        assert "upstream_request_id" not in completed

    def test_oversized_inbound_id_is_truncated_in_upstream_field(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/", HTTP_X_REQUEST_ID="a" * 1000))

        completed = logger.events_for("request_completed")[0]
        # MAX_REQUEST_ID is 200 in _limits.py.
        assert len(completed["upstream_request_id"]) == 200
        # The locally-minted request_id is unaffected.
        assert UUID_HEX_RE.match(completed["request_id"])

    @pytest.mark.parametrize(
        "bad_value",
        [
            "has spaces",
            "has\ttab",
            "has\x00null",
            'has"quote',
            "has'apostrophe",
            "  ",  # whitespace-only → stripped to empty → rejected
        ],
    )
    def test_charset_invalid_inbound_id_is_rejected_without_upstream_field(
        self, bad_value, rf, logger, settings_sandbox
    ):
        """Security finding A3: charset-invalid X-Request-ID values must
        NOT be propagated verbatim into log context (log-injection /
        log-search hijack). The local request_id is still minted cleanly.
        """
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/", HTTP_X_REQUEST_ID=bad_value))

        completed = logger.events_for("request_completed")[0]
        assert "upstream_request_id" not in completed
        assert UUID_HEX_RE.match(completed["request_id"])

    def test_empty_inbound_header_produces_no_upstream_field(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/", HTTP_X_REQUEST_ID=""))

        completed = logger.events_for("request_completed")[0]
        assert "upstream_request_id" not in completed
        assert UUID_HEX_RE.match(completed["request_id"])

    def test_request_id_distinct_across_two_sequential_requests(
        self, rf, logger, settings_sandbox
    ):
        """Per-hop identity: two requests through the same middleware
        instance get two different request_ids even when the inbound
        header is identical.
        """
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/", HTTP_X_REQUEST_ID="upstream-xyz"))
        first_id = logger.events_for("request_completed")[0]["request_id"]

        mw(rf.get("/", HTTP_X_REQUEST_ID="upstream-xyz"))
        second_id = logger.events_for("request_completed")[1]["request_id"]

        assert first_id != second_id


# ---------------------------------------------------------------------------
# Scope ownership
# ---------------------------------------------------------------------------


class TestScopeOwnership:
    def test_scope_token_released_after_successful_request(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/"))
        # After request, no owner should be bound.
        assert _request_scope_owner.get() is None

    def test_scope_token_released_after_exception(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def boom(_r):
            msg = "no"
            raise ValueError(msg)

        mw = StructuredLoggingMiddlewareOTel(boom)
        with pytest.raises(ValueError, match="no"):
            mw(rf.get("/"))
        assert _request_scope_owner.get() is None


# ---------------------------------------------------------------------------
# Enablement flag
# ---------------------------------------------------------------------------


class TestEnablementFlag:
    def test_disabled_flag_raises_middleware_not_used(self, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_MIDDLEWARE_ENABLED=False,
        )
        with pytest.raises(MiddlewareNotUsed):
            StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))


# ---------------------------------------------------------------------------
# Http404 carve-out (Art. 46)
# ---------------------------------------------------------------------------


class TestHttp404CarveOut:
    """Constitution Art. 46: ``Http404`` MUST be logged at INFO or WARNING,
    never ERROR. Treated as ordinary traffic (``request_completed``), not a
    server-side failure (``request_failed``).
    """

    def test_http404_logged_at_warning_not_error(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def not_found(_request):
            msg = "nope"
            raise Http404(msg)

        mw = StructuredLoggingMiddlewareOTel(not_found)
        with pytest.raises(Http404):
            mw(rf.get("/missing"))

        # Emitted as request_completed, NOT request_failed.
        completed = logger.events_for("request_completed")
        failed = logger.events_for("request_failed")
        assert len(completed) == 1
        assert len(failed) == 0
        # WARNING, not ERROR. No traceback.
        assert completed[0]["log_level"] == "warning"
        assert completed[0]["exception_captured"] is False
        # Synthesised 404, not 500.
        assert completed[0]["http.response.status_code"] == 404
        assert completed[0]["exception.type"] == "Http404"
        assert completed[0]["error.type"] == "404"

    def test_http404_reraised_after_logging(self, rf, logger, settings_sandbox):
        """Art. 43: exception MUST be re-raised so Django's error path
        (404 handler, debug toolbar) still sees it.
        """
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def not_found(_request):
            raise Http404

        mw = StructuredLoggingMiddlewareOTel(not_found)
        with pytest.raises(Http404):
            mw(rf.get("/missing"))


# ---------------------------------------------------------------------------
# `error.type` semconv field (Art. 50)
# ---------------------------------------------------------------------------


class TestErrorType:
    """OTel HTTP semconv note 4: ``error.type`` carries the status code
    string on 4xx/5xx responses and the fully-qualified exception class
    name on the exception path. Must be absent on 2xx/3xx.
    """

    def test_error_type_absent_on_2xx(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/"))
        completed = logger.events_for("request_completed")[0]
        assert "error.type" not in completed

    def test_error_type_absent_on_3xx(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=301))
        mw(rf.get("/"))
        completed = logger.events_for("request_completed")[0]
        assert "error.type" not in completed

    def test_error_type_is_status_string_on_4xx(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=403))
        mw(rf.get("/"))
        completed = logger.events_for("request_completed")[0]
        assert completed["error.type"] == "403"

    def test_error_type_is_status_string_on_5xx(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=502))
        mw(rf.get("/"))
        completed = logger.events_for("request_completed")[0]
        assert completed["error.type"] == "502"

    def test_error_type_is_fqn_on_unhandled_exception(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def boom(_r):
            msg = "broken"
            raise RuntimeError(msg)

        mw = StructuredLoggingMiddlewareOTel(boom)
        with pytest.raises(RuntimeError, match="broken"):
            mw(rf.get("/"))

        failed = logger.events_for("request_failed")[0]
        assert failed["error.type"] == "builtins.RuntimeError"
        assert failed["exception.type"] == "RuntimeError"


# ---------------------------------------------------------------------------
# Finally-based emission idempotency (Art. 40, 47)
# ---------------------------------------------------------------------------


class TestEmissionIdempotency:
    """Exactly one ``request_started`` + exactly one of
    (``request_completed`` | ``request_failed``) per non-excluded request,
    regardless of control-flow path (success, Http404, unhandled exception).
    """

    def test_success_emits_exactly_one_started_and_one_completed(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/"))

        assert len(logger.events_for("request_started")) == 1
        assert len(logger.events_for("request_completed")) == 1
        assert len(logger.events_for("request_failed")) == 0

    def test_http404_emits_exactly_one_started_and_one_completed(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def not_found(_r):
            raise Http404

        mw = StructuredLoggingMiddlewareOTel(not_found)
        with pytest.raises(Http404):
            mw(rf.get("/"))

        assert len(logger.events_for("request_started")) == 1
        assert len(logger.events_for("request_completed")) == 1
        assert len(logger.events_for("request_failed")) == 0

    def test_exception_emits_exactly_one_started_and_one_failed(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def boom(_r):
            raise RuntimeError

        mw = StructuredLoggingMiddlewareOTel(boom)
        with pytest.raises(RuntimeError):
            mw(rf.get("/"))

        assert len(logger.events_for("request_started")) == 1
        assert len(logger.events_for("request_completed")) == 0
        assert len(logger.events_for("request_failed")) == 1


# ---------------------------------------------------------------------------
# Schema version (Art. 51)
# ---------------------------------------------------------------------------


class TestSchemaVersion:
    """``logging_schema_version`` MUST be bound to every event so downstream
    consumers can detect breaking changes without grepping source.
    """

    def test_schema_version_on_startup_event(self, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        startup = logger.events_for("structured_logging_middleware_otel_active")
        assert len(startup) == 1
        assert startup[0]["logging_schema_version"] == "1.0"

    def test_schema_version_on_request_events(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/"))

        for event_name in ("request_started", "request_completed"):
            events = logger.events_for(event_name)
            assert len(events) == 1
            assert events[0]["logging_schema_version"] == "1.0"

    def test_schema_version_on_request_failed(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def boom(_r):
            raise RuntimeError

        mw = StructuredLoggingMiddlewareOTel(boom)
        with pytest.raises(RuntimeError):
            mw(rf.get("/"))

        failed = logger.events_for("request_failed")[0]
        assert failed["logging_schema_version"] == "1.0"


# ---------------------------------------------------------------------------
# Settings validation (Art. 15)
# ---------------------------------------------------------------------------


class TestSettingsValidation:
    """Malformed settings MUST produce ``ImproperlyConfigured`` with a
    specific message, never a bare ``TypeError`` / ``AttributeError`` /
    ``re.error``.
    """

    def test_non_iterable_excluded_prefixes_raises_improperly_configured(
        self, logger, settings_sandbox
    ):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_EXCLUDED_PREFIXES=12345,  # not iterable
        )
        with pytest.raises(ImproperlyConfigured, match="iterable"):
            StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))

    def test_invalid_regex_in_excluded_regexes_raises_improperly_configured(
        self, logger, settings_sandbox
    ):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_EXCLUDED_REGEXES=("[unbalanced",),
        )
        with pytest.raises(ImproperlyConfigured, match="invalid regex"):
            StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))

    def test_non_iterable_header_allowlist_raises_improperly_configured(
        self, logger, settings_sandbox
    ):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=42,  # not iterable
        )
        with pytest.raises(ImproperlyConfigured, match="iterable"):
            StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))

    def test_non_string_allowlist_entries_are_silently_skipped(
        self, rf, logger, settings_sandbox
    ):
        """Copy-paste bugs (``None`` / ``int`` in the allowlist) must not
        brick middleware startup. Drop silently and carry on.
        """
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=("X-OK", None, 42, "X-Also-OK"),
        )
        # Must not raise.
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        mw(rf.get("/", HTTP_X_OK="yes", HTTP_X_ALSO_OK="also-yes"))
        completed = logger.events_for("request_completed")[0]
        assert completed["http.request.header.x_ok"] == "yes"
        assert completed["http.request.header.x_also_ok"] == "also-yes"


# ---------------------------------------------------------------------------
# Forbidden header rejection warning (Art. 53)
# ---------------------------------------------------------------------------


class TestForbiddenHeaderRejectionWarning:
    def test_rejection_warning_emitted_at_startup(self, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=("Authorization", "X-OK", "Cookie"),
        )
        StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        rejected = logger.events_for(
            "structured_logging_middleware_otel_forbidden_headers_rejected"
        )
        assert len(rejected) == 1
        assert "Authorization" in rejected[0]["rejected"]
        assert "Cookie" in rejected[0]["rejected"]
        assert "X-OK" not in rejected[0]["rejected"]

    def test_no_warning_when_allowlist_has_no_forbidden_entries(
        self, logger, settings_sandbox
    ):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=("X-Tenant-ID", "X-Request-Origin"),
        )
        StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        rejected = logger.events_for(
            "structured_logging_middleware_otel_forbidden_headers_rejected"
        )
        assert len(rejected) == 0

    def test_case_insensitive_rejection(self, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=("AUTHORIZATION", "CoOkIe"),
        )
        StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        rejected = logger.events_for(
            "structured_logging_middleware_otel_forbidden_headers_rejected"
        )
        assert len(rejected) == 1
        assert set(rejected[0]["rejected"]) == {"AUTHORIZATION", "CoOkIe"}


# ---------------------------------------------------------------------------
# Trace-context precedence via OTel propagator (Art. 60)
# ---------------------------------------------------------------------------


class TestTraceContextPrecedence:
    """Art. 60: the ``trace_id`` precedence ladder MUST have a dedicated
    test. With the OTel middleware, precedence is delegated to the
    composite propagator (W3C + X-Ray), where W3C wins when both are
    present. We verify via the span attached to the recorded server span,
    which is the source of truth for ``trace_id`` in this middleware.
    """

    def test_w3c_traceparent_wins_over_amzn_header(
        self, logger, settings_sandbox
    ):
        from django.http import HttpResponse as _HttpResponse  # noqa: PLC0415
        from django.test import Client  # noqa: PLC0415
        from django.urls import clear_url_caches, path  # noqa: PLC0415

        def _ok(_request):
            return _HttpResponse(status=200)

        otel_mw = [
            m for m in django_settings.MIDDLEWARE if "opentelemetry" in m.lower()
        ]
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            ROOT_URLCONF=__name__,
            MIDDLEWARE=[
                *otel_mw,
                "i_dot_ai_utilities.logging.middleware.django_otel.StructuredLoggingMiddlewareOTel",
            ],
        )
        global urlpatterns  # noqa: PLW0603
        prev = urlpatterns
        urlpatterns = [path("trace-test", _ok)]
        clear_url_caches()
        w3c_trace_id = "0af7651916cd43dd8448eb211c80319c"
        w3c_span_id = "b7ad6b7169203331"
        try:
            client = Client()
            response = client.get(
                "/trace-test",
                HTTP_TRACEPARENT=f"00-{w3c_trace_id}-{w3c_span_id}-01",
                # A fully-formed X-Ray header that, if it won, would
                # override the trace id with a different value. The
                # composite propagator is ordered so W3C extracts last
                # and therefore wins.
                HTTP_X_AMZN_TRACE_ID=(
                    "Root=1-6758db72-1234567890abcdef12345678;"
                    "Parent=53995c3f42cd8ad8;Sampled=1"
                ),
            )
        finally:
            urlpatterns = prev
            clear_url_caches()

        assert response.status_code == 200
        spans = _SHARED_EXPORTER.get_finished_spans()
        assert spans, "DjangoInstrumentor did not emit a span"
        # The server span's trace id must equal the W3C-provided trace id
        # (32 hex chars, lowercase).
        server_span = spans[-1]
        actual_hex = format(server_span.get_span_context().trace_id, "032x")
        assert actual_hex == w3c_trace_id

    def test_amzn_header_used_when_no_traceparent(
        self, logger, settings_sandbox
    ):
        from django.http import HttpResponse as _HttpResponse  # noqa: PLC0415
        from django.test import Client  # noqa: PLC0415
        from django.urls import clear_url_caches, path  # noqa: PLC0415

        def _ok(_request):
            return _HttpResponse(status=200)

        otel_mw = [
            m for m in django_settings.MIDDLEWARE if "opentelemetry" in m.lower()
        ]
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            ROOT_URLCONF=__name__,
            MIDDLEWARE=[
                *otel_mw,
                "i_dot_ai_utilities.logging.middleware.django_otel.StructuredLoggingMiddlewareOTel",
            ],
        )
        global urlpatterns  # noqa: PLW0603
        prev = urlpatterns
        urlpatterns = [path("xray-test", _ok)]
        clear_url_caches()
        # Well-formed X-Ray Root: 1-<8 hex epoch>-<24 hex random>.
        # When concatenated (epoch || random) you get a valid 32-hex id.
        # The AWS propagator requires Parent= and Sampled= fields too;
        # Root alone is not sufficient for the propagator to accept the
        # span context as valid.
        amzn_epoch = "6758db72"
        amzn_random = "1234567890abcdef12345678"
        amzn_parent = "53995c3f42cd8ad8"
        try:
            client = Client()
            response = client.get(
                "/xray-test",
                HTTP_X_AMZN_TRACE_ID=(
                    f"Root=1-{amzn_epoch}-{amzn_random};"
                    f"Parent={amzn_parent};Sampled=1"
                ),
            )
        finally:
            urlpatterns = prev
            clear_url_caches()

        assert response.status_code == 200
        spans = _SHARED_EXPORTER.get_finished_spans()
        assert spans, "DjangoInstrumentor did not emit a span"
        server_span = spans[-1]
        actual_hex = format(server_span.get_span_context().trace_id, "032x")
        # X-Ray trace id is the 8+24 hex after the version prefix.
        assert actual_hex == f"{amzn_epoch}{amzn_random}"


# ---------------------------------------------------------------------------
# Process-exception hook contract (Art. 45)
# ---------------------------------------------------------------------------


class TestProcessExceptionSafety:
    def test_process_exception_never_raises_and_writes_nothing(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: HttpResponse(status=200))
        # Clear context and verify no writes happen via the hook.
        logger.context = {}
        events_before = len(logger.events)
        result = mw.process_exception(rf.get("/"), RuntimeError("x"))
        assert result is None
        assert len(logger.events) == events_before
        assert logger.context == {}


# ---------------------------------------------------------------------------
# Middleware-surface contract (Art. 1, 2)
# ---------------------------------------------------------------------------


class TestMiddlewareSurface:
    def test_sync_flags_are_set(self):
        assert StructuredLoggingMiddlewareOTel.sync_capable is True
        assert StructuredLoggingMiddlewareOTel.async_capable is False

    def test_call_returns_response_unchanged(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        sentinel = HttpResponse(status=204)
        mw = StructuredLoggingMiddlewareOTel(lambda _r: sentinel)
        assert mw(rf.get("/")) is sentinel
