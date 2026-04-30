# mypy: disable-error-code="no-untyped-def"
"""Integration tests for ``StructuredLoggingMiddleware``.

Uses Django's ``RequestFactory`` to build minimal requests. Skipped entirely
when Django is not installed.

Rather than fighting structlog's per-instance re-configuration, we inject a
``RecordingLogger`` double via the ``I_DOT_AI_LOGGER`` setting. The double
implements the same contract the middleware depends on and captures every
call verbatim, which lets us assert on structured fields without touching
structlog at all.
"""

from __future__ import annotations

import re
from typing import Any
from unittest.mock import Mock

import pytest

django = pytest.importorskip("django")

# Must configure Django settings BEFORE importing anything that touches
# django.conf.settings or django.http.
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
from django.core.exceptions import ImproperlyConfigured, MiddlewareNotUsed  # type: ignore[import-untyped]  # noqa: E402
from django.http import Http404, HttpResponse  # type: ignore[import-untyped]  # noqa: E402
from django.test import RequestFactory  # type: ignore[import-untyped]  # noqa: E402

from i_dot_ai_utilities.logging.enrichers.django_enricher import (  # noqa: E402
    DjangoEnricher,
)
from i_dot_ai_utilities.logging.middleware.django import (  # noqa: E402
    StructuredLoggingMiddleware,
)
from i_dot_ai_utilities.logging.structured_logger import (  # noqa: E402
    _request_scope_owner,
)

urlpatterns: list = []

UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")

VALID_TP_V00 = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
VALID_TRACE_ID = "a" * 32
VALID_SPAN_ID = "b" * 16


# ---------------------------------------------------------------------------
# Test double: records every call made by the middleware.
# ---------------------------------------------------------------------------


class RecordingLogger:
    """Structured-logger double that collects everything the middleware does.

    Matches the duck-typed contract in ``_settings._StructuredLoggerLike``.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.context: dict[str, Any] = {}
        self.refresh_calls: list[list[dict[str, Any]] | None] = []

    # --- structured logger API ---------------------------------------------

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
        context_enrichers: list[dict[str, Any]] | None = None,
        scope: str = "manual",  # noqa: ARG002 - test double mirrors new kwarg
    ) -> None:
        self.refresh_calls.append(context_enrichers)
        # Real refresh_context clears prior state; mirror that so leak tests
        # behave realistically.
        self.context = {}
        # Invoke the real ``DjangoEnricher`` so middleware integration tests
        # verify the end-to-end field pipeline (enricher -> refresh_context ->
        # bound context), mirroring what the production ``StructuredLogger``
        # does. The real logger calls ``bind_contextvars`` with the enricher's
        # output; we store the same values into ``self.context`` so later
        # ``_record`` calls include them.
        if not context_enrichers:
            return
        for spec in context_enrichers:
            enricher_type = spec.get("type")
            target = spec.get("object")
            if enricher_type is None or target is None:
                continue
            if enricher_type.name == "DJANGO":
                extracted = DjangoEnricher().extract_context(self, target)
                if extracted:
                    self.context.update(extracted)

    # --- internals ---------------------------------------------------------

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

    # --- convenience -------------------------------------------------------

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
    """Restore any attribute changes on ``django.conf.settings`` after the test."""
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
# T16: happy path
# ---------------------------------------------------------------------------


class TestHappyPath:
    def test_200_emits_started_and_completed(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        response_sentinel = HttpResponse(status=200)

        def get_response(_request):
            return response_sentinel

        middleware = StructuredLoggingMiddleware(get_response)
        request = rf.get("/users")
        result = middleware(request)

        # (Art. 3) response returned unchanged.
        assert result is response_sentinel

        # (Art. 47) exactly one started + one completed, no failed.
        started = logger.events_for("request_started")
        completed = logger.events_for("request_completed")
        failed = logger.events_for("request_failed")
        assert len(started) == 1
        assert len(completed) == 1
        assert len(failed) == 0

        # (Art. 49) INFO level on both.
        assert started[0]["log_level"] == "info"
        assert completed[0]["log_level"] == "info"

        # (Art. 50) OTel field names populated.
        assert completed[0]["http.response.status_code"] == 200
        assert completed[0]["http.request.method"] == "GET"
        assert completed[0]["url.path"] == "/users"
        assert completed[0]["url.scheme"] in ("http", "https")

        # (Art. 39) integer duration_ms present and non-negative.
        assert isinstance(completed[0]["duration_ms"], int)
        assert completed[0]["duration_ms"] >= 0

        # (Art. 31, 32) trace_id + request_id always bound.
        assert UUID_HEX_RE.match(completed[0]["trace_id"])
        assert UUID_HEX_RE.match(completed[0]["request_id"])

    def test_refresh_context_called_first_with_django_enricher(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(rf.get("/anything"))

        # Ignore the startup log's refresh call (there isn't one; refresh_context
        # is called in __call__, not __init__).
        assert len(logger.refresh_calls) == 1
        enrichers = logger.refresh_calls[0]
        assert enrichers is not None
        assert len(enrichers) == 1
        assert enrichers[0]["type"].name == "DJANGO"


# ---------------------------------------------------------------------------
# T17: status-driven levels
# ---------------------------------------------------------------------------


class TestStatusLevels:
    @pytest.mark.parametrize(
        ("status", "expected_level"),
        [(404, "warning"), (500, "error")],
    )
    def test_level_reflects_status(self, status, expected_level, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=status))
        middleware(rf.get("/page"))

        completed = logger.events_for("request_completed")
        assert len(completed) == 1
        assert completed[0]["log_level"] == expected_level
        assert completed[0]["http.response.status_code"] == status
        # (Art. 44) Completed event fires when the view returned normally;
        # request_failed must NOT fire here.
        assert logger.events_for("request_failed") == []
        # The exception flag must be False on the completed event.
        assert completed[0]["exception_captured"] is False


# ---------------------------------------------------------------------------
# T18: exception path + contextvars leak
# ---------------------------------------------------------------------------


class TestExceptionPath:
    def test_exception_logged_and_reraised(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        original_exc = RuntimeError("boom")

        get_response = Mock(side_effect=original_exc)
        middleware = StructuredLoggingMiddleware(get_response)

        with pytest.raises(RuntimeError) as excinfo:
            middleware(rf.get("/broken"))

        # (Art. 41) Bare ``raise`` preserves the identity of the exception.
        assert excinfo.value is original_exc

        # (Art. 47) exactly one request_failed, no request_completed.
        failed = logger.events_for("request_failed")
        assert len(failed) == 1
        assert logger.events_for("request_completed") == []

        # (Art. 49) ERROR level.
        assert failed[0]["log_level"] == "error"

        # (Art. 44) status synthesised to 500 and exception.type bound.
        assert failed[0]["http.response.status_code"] == 500
        assert failed[0]["exception.type"] == "RuntimeError"

        # (Art. 41) logged via .exception() — our double marks this flag.
        assert failed[0]["exception_captured"] is True


class TestContextLeak:
    def test_context_does_not_leak_between_requests(self, rf, logger, settings_sandbox):
        """Two consecutive requests with different trace headers must not share
        context. Validates that ``refresh_context`` runs first in ``__call__``.
        """
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))

        # Hex-shaped request ids so they qualify for the trace_id slot under
        # the A3-hardened precedence rules.
        first_id = "0123456789abcdef0123456789abcdef"
        second_id = "fedcba9876543210fedcba9876543210"

        # First request: custom hex request-id.
        middleware(rf.get("/one", headers={"X-Request-ID": first_id}))

        first_events = list(logger.events)
        logger.events.clear()

        # Second request: different hex request-id, no traceparent.
        middleware(rf.get("/two", headers={"X-Request-ID": second_id}))
        completed_two = logger.events_for("request_completed")
        assert len(completed_two) == 1

        # The second request's context MUST NOT contain the first request's
        # upstream_request_id.
        assert completed_two[0].get("upstream_request_id") == second_id
        assert completed_two[0].get("trace_id") == second_id
        assert completed_two[0]["url.path"] == "/two"

        # Sanity: first request had the correct id.
        assert first_events[-1].get("upstream_request_id") == first_id


# ---------------------------------------------------------------------------
# T19: exclusion skip + trace_id precedence ladder
# ---------------------------------------------------------------------------


class TestExclusionSkip:
    def test_healthz_produces_no_log_events(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        response = HttpResponse(status=200)

        middleware = StructuredLoggingMiddleware(lambda _request: response)
        result = middleware(rf.get("/healthz"))

        # (Art. 48) no request_started or request_completed at all.
        assert logger.events_for("request_started") == []
        assert logger.events_for("request_completed") == []
        assert logger.events_for("request_failed") == []

        # Response still flows through unchanged.
        assert result is response

    def test_custom_exclusion_prefixes_override_defaults(self, rf, logger, settings_sandbox):
        # Override the defaults entirely with a single custom prefix;
        # /healthz should then NOT be excluded (not in the override).
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_EXCLUDED_PREFIXES=("/internal/",),
        )

        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))

        # /healthz is NOT in the override, so it IS logged.
        middleware(rf.get("/healthz"))
        assert len(logger.events_for("request_completed")) == 1

        logger.events.clear()

        # /internal/ping IS excluded.
        middleware(rf.get("/internal/ping"))
        assert logger.events_for("request_completed") == []


class TestTraceIdPrecedence:
    def test_traceparent_wins(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(
            rf.get(
                "/",
                headers={
                    "traceparent": VALID_TP_V00,
                    "X-Amzn-Trace-Id": "Root=1-aws-id;Sampled=1",
                    "X-Request-ID": "upstream-req",
                },
            )
        )
        completed = logger.events_for("request_completed")[0]
        assert completed["trace_id"] == VALID_TRACE_ID
        assert completed["span_id"] == VALID_SPAN_ID
        assert completed["amzn_trace_root"] == "1-aws-id"
        assert completed["upstream_request_id"] == "upstream-req"
        assert completed["trace_id_source"] == "traceparent"

    def test_amzn_when_traceparent_absent(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(
            rf.get(
                "/",
                headers={
                    "X-Amzn-Trace-Id": "Root=1-aws-id",
                    "X-Request-ID": "upstream-req",
                },
            )
        )
        completed = logger.events_for("request_completed")[0]
        assert completed["trace_id"] == "1-aws-id"
        assert "span_id" not in completed
        assert completed["trace_id_source"] == "amzn"

    def test_request_id_when_only_header_and_hex_shaped(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        hex_id = "cafef00dcafef00dcafef00dcafef00d"
        middleware(rf.get("/", headers={"X-Request-ID": hex_id}))
        completed = logger.events_for("request_completed")[0]
        assert completed["trace_id"] == hex_id
        assert "span_id" not in completed
        assert completed["trace_id_source"] == "request_id"

    def test_non_hex_request_id_does_not_hijack_trace_id(self, rf, logger, settings_sandbox):
        # Security (A3): an attacker-controlled non-hex X-Request-ID must
        # never become trace_id. It is preserved for debugging only.
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(rf.get("/", headers={"X-Request-ID": "rid-42"}))
        completed = logger.events_for("request_completed")[0]
        assert completed["upstream_request_id"] == "rid-42"
        assert completed["trace_id"] != "rid-42"
        assert UUID_HEX_RE.match(completed["trace_id"])
        assert completed["trace_id_source"] == "synthetic"

    def test_uuid_generated_when_no_headers(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(rf.get("/"))
        completed = logger.events_for("request_completed")[0]
        assert UUID_HEX_RE.match(completed["trace_id"])
        assert "span_id" not in completed
        assert completed["trace_id_source"] == "synthetic"
        # Fresh per-hop request_id always present.
        assert UUID_HEX_RE.match(completed["request_id"])
        # Different ids.
        assert completed["trace_id"] != completed["request_id"]


# ---------------------------------------------------------------------------
# Additional: startup log, sync flags, process_exception safety
# ---------------------------------------------------------------------------


class TestStartupAndFlags:
    def test_sync_flags(self):
        assert StructuredLoggingMiddleware.sync_capable is True
        assert StructuredLoggingMiddleware.async_capable is False

    def test_startup_log_emitted(self, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        startup_events = logger.events_for("structured_logging_middleware_active")
        assert len(startup_events) == 1

    def test_middleware_not_used_when_disabled(self, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_MIDDLEWARE_ENABLED=False,
        )
        with pytest.raises(MiddlewareNotUsed):
            StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))


class TestProcessExceptionSafety:
    def test_process_exception_never_raises(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        # No resolver_match set on the request; process_exception must tolerate.
        result = middleware.process_exception(rf.get("/"), RuntimeError("x"))
        assert result is None

    def test_process_exception_writes_nothing_to_context(self, rf, logger, settings_sandbox):
        """Security regression test (A7): ``process_exception`` must not
        mutate the logger context. Writes made there would bleed into the
        next request's logs if a later middleware swallows the exception,
        or persist via contextvars until the next ``refresh_context``.
        """
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        # Drop the startup log so the post-condition reads cleanly.
        logger.events.clear()

        class _FakeResolverMatch:
            view_name = "app.views.poisoned"
            url_name = "poisoned"

        request = rf.get("/x")
        request.resolver_match = _FakeResolverMatch()

        # Starting state: logger context is empty.
        assert logger.context == {}

        # Invoking process_exception directly must leave context untouched
        # even when a tempting ``resolver_match`` is present.
        middleware.process_exception(request, RuntimeError("x"))

        assert logger.context == {}
        # And no log events should have been produced.
        assert logger.events == []


class TestRequestScopeOwnership:
    """Security finding (residual): manual ``refresh_context()`` calls inside
    the middleware's request scope must not silently de-correlate logs.
    """

    def test_scope_released_after_request(self, rf, logger, settings_sandbox):
        """After ``__call__`` returns, the ownership token must be reset so
        unrelated logger usage between requests behaves normally."""
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        assert _request_scope_owner.get() is None
        middleware(rf.get("/anything"))
        assert _request_scope_owner.get() is None

    def test_scope_released_after_exception(self, rf, logger, settings_sandbox):
        """The ``finally`` block must release the scope even when the view
        raises, otherwise subsequent requests would see a stale owner."""
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def boom(_request):
            msg = "x"
            raise RuntimeError(msg)

        middleware = StructuredLoggingMiddleware(boom)
        with pytest.raises(RuntimeError):
            middleware(rf.get("/boom"))
        assert _request_scope_owner.get() is None

    def test_middleware_passes_request_scope_to_refresh_context(self, rf, settings_sandbox):
        """Regression: the middleware must explicitly pass ``scope='request'``
        to ``refresh_context`` so the ownership-aware branch fires."""

        class _ScopeRecordingLogger(RecordingLogger):
            def __init__(self) -> None:
                super().__init__()
                self.refresh_scopes: list[str] = []

            def refresh_context(
                self,
                context_enrichers=None,
                scope: str = "manual",
            ) -> None:
                self.refresh_scopes.append(scope)
                super().refresh_context(context_enrichers=context_enrichers)

        recording = _ScopeRecordingLogger()
        settings_sandbox(I_DOT_AI_LOGGER=recording)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(rf.get("/x"))
        # A single refresh, explicitly scoped as a request.
        assert recording.refresh_scopes == ["request"]


class TestEnricherIntegration:
    """End-to-end: enricher-sourced fields reach the log line via the middleware."""

    def test_enricher_fields_present_on_completed_event(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(
            rf.get(
                "/users?page=2",
                headers={
                    "User-Agent": "integration-test/1.0",
                    "X-Forwarded-For": "203.0.113.7",
                },
            )
        )

        completed = logger.events_for("request_completed")[0]
        # Sourced from the enricher, verified to flow through the real code path.
        assert completed["http.request.method"] == "GET"
        assert completed["url.path"] == "/users"
        assert completed["url.query"] == "page=2"
        assert completed["user_agent.original"] == "integration-test/1.0"
        assert completed["http.request.header.x_forwarded_for"] == "203.0.113.7"
        # ``server.address`` always emitted by the enricher.
        assert "server.address" in completed

    def test_route_bound_on_completion_after_resolver_runs(self, rf, logger, settings_sandbox):
        """Enricher runs before URL routing; middleware re-reads resolver_match
        on completion so the route fields reflect the matched view."""
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        class _FakeResolverMatch:
            view_name = "app.views.detail"
            url_name = "detail"

        def get_response(request):
            # Simulate Django's URL resolver running between refresh_context
            # and the response.
            request.resolver_match = _FakeResolverMatch()
            return HttpResponse(status=200)

        middleware = StructuredLoggingMiddleware(get_response)
        middleware(rf.get("/thing"))

        completed = logger.events_for("request_completed")[0]
        assert completed["http.route"] == "app.views.detail"
        assert completed["django.url_name"] == "detail"

    def test_route_bound_on_failure_after_resolver_runs(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        class _FakeResolverMatch:
            view_name = "app.views.broken"
            url_name = "broken"

        def get_response(request):
            request.resolver_match = _FakeResolverMatch()
            msg = "kaboom"
            raise RuntimeError(msg)

        middleware = StructuredLoggingMiddleware(get_response)
        with pytest.raises(RuntimeError):
            middleware(rf.get("/thing"))

        failed = logger.events_for("request_failed")[0]
        assert failed["http.route"] == "app.views.broken"
        assert failed["django.url_name"] == "broken"


class TestHeaderAllowlist:
    def test_allowlisted_headers_bound_on_completed(self, rf, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=("X-Tenant-ID",),
        )
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(rf.get("/", headers={"X-Tenant-ID": "tenant-42"}))

        completed = logger.events_for("request_completed")[0]
        assert completed["http.request.header.x_tenant_id"] == "tenant-42"

    def test_absent_allowlisted_header_not_bound(self, rf, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=("X-Tenant-ID",),
        )
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(rf.get("/"))

        completed = logger.events_for("request_completed")[0]
        assert "http.request.header.x_tenant_id" not in completed


class TestLoggerResolutionSecurity:
    """Security finding A4: ``I_DOT_AI_LOGGER`` must not accept dotted-import
    strings. A string would let any process able to influence Django settings
    import arbitrary modules at middleware boot.
    """

    def test_dotted_string_is_rejected(self, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER="some.module.path.logger")
        with pytest.raises(ImproperlyConfigured) as excinfo:
            StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        # Error message must be actionable — point the operator at the
        # supported forms and explain the security rationale.
        msg = str(excinfo.value)
        assert "dotted-import string" in msg
        assert "callable" in msg.lower() or "logger object" in msg.lower()

    def test_logger_object_is_accepted(self, logger, settings_sandbox):
        """Pre-constructed logger instances remain supported."""
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        # No exception.
        mw = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        assert mw is not None

    def test_zero_arg_callable_is_accepted(self, logger, settings_sandbox):
        """Zero-arg factories remain supported for lazy construction."""
        settings_sandbox(I_DOT_AI_LOGGER=lambda: logger)
        mw = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        assert mw is not None

    def test_unsupported_type_is_rejected(self, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=12345)
        with pytest.raises(ImproperlyConfigured) as excinfo:
            StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        assert "callable" in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Constitution remediation tests (Art. 22, 40, 46, 50, 51, 52, 54)
# ---------------------------------------------------------------------------


class TestHttp404CarveOut:
    """Art. 46: Http404 MUST be logged at INFO or WARNING, never ERROR.

    We pick WARNING to mirror ``level_for_status(404) -> warning`` so
    consumers see a single rule "4xx = warning" regardless of whether the
    404 came from a direct ``HttpResponse(status=404)`` or a raised
    ``Http404``.
    """

    def test_http404_logged_at_warning_not_error(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def view(_request):
            raise Http404

        middleware = StructuredLoggingMiddleware(view)
        with pytest.raises(Http404):
            middleware(rf.get("/missing"))

        # No request_failed — Http404 is ordinary 4xx traffic.
        assert logger.events_for("request_failed") == []

        completed = logger.events_for("request_completed")
        assert len(completed) == 1
        event = completed[0]
        # (Art. 46) WARNING, never ERROR.
        assert event["log_level"] == "warning"
        # Synthetic status 404, not 500.
        assert event["http.response.status_code"] == 404
        # OTel ``error.type`` uses the status code string on 4xx responses.
        assert event["error.type"] == "404"
        # Simple exception type name, consistent with the unhandled-exception
        # path.
        assert event["exception.type"] == "Http404"

    def test_http404_reraised_after_logging(self, rf, logger, settings_sandbox):
        """The middleware must re-raise so Django's own 404 handler still
        runs (Art. 43: exceptions MUST NEVER be swallowed)."""
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        original = Http404("nope")

        def view(_request):
            raise original

        middleware = StructuredLoggingMiddleware(view)
        with pytest.raises(Http404) as excinfo:
            middleware(rf.get("/missing"))
        assert excinfo.value is original


class TestOTelErrorType:
    """Art. 50: OTel alignment — bind ``error.type`` on every failing path."""

    def test_error_type_bound_on_500_response(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=500))
        middleware(rf.get("/boom"))
        completed = logger.events_for("request_completed")[0]
        # HTTP status code stringified per OTel HTTP semconv note 4.
        assert completed["error.type"] == "500"

    def test_error_type_bound_on_unhandled_exception_is_fqn(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        def view(_request):
            msg = "boom"
            raise RuntimeError(msg)

        middleware = StructuredLoggingMiddleware(view)
        with pytest.raises(RuntimeError):
            middleware(rf.get("/boom"))
        failed = logger.events_for("request_failed")[0]
        # OTel HTTP semconv: exception type is the fully-qualified class name.
        # ``builtins.RuntimeError`` for a stdlib exception.
        assert failed["error.type"] == "builtins.RuntimeError"
        # ``exception.type`` remains the simple name for ergonomics.
        assert failed["exception.type"] == "RuntimeError"

    def test_error_type_absent_on_2xx(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(rf.get("/ok"))
        completed = logger.events_for("request_completed")[0]
        # No error ⇒ no error.type (per OTel semconv "SHOULD NOT set").
        assert "error.type" not in completed


class TestUserIdOtelRename:
    """Art. 50: OTel deprecated ``enduser.*`` in v1.24; we emit ``user.id``
    only, and never the authentication flag (per Art. 55, pk only / no PII).
    """

    def test_user_id_field_used_for_authenticated_user(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)

        class _FakeUser:
            pk = 42
            id = 42
            is_authenticated = True

        def view(request):
            request.user = _FakeUser()
            # Re-run enricher after assigning user (auth middleware would
            # ordinarily do this before our middleware; here we assert the
            # field surfaces once auth has populated user).
            return HttpResponse(status=200)

        middleware = StructuredLoggingMiddleware(view)
        # Attach user before ``__call__`` runs so the enricher sees it.
        request = rf.get("/me")
        request.user = _FakeUser()
        middleware(request)

        completed = logger.events_for("request_completed")[0]
        assert completed.get("user.id") == "42"
        # ``enduser.*`` namespace removed.
        assert "enduser.id" not in completed
        assert "enduser.authenticated" not in completed


class TestSchemaVersion:
    """Art. 51: schema MUST be documented AND versioned."""

    def test_schema_version_bound_on_every_event(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        # Startup event emits its own ``logging_schema_version`` kwarg.
        startup = logger.events_for("structured_logging_middleware_active")[0]
        assert startup["logging_schema_version"] == "1.0"

        middleware(rf.get("/x"))
        started = logger.events_for("request_started")[0]
        completed = logger.events_for("request_completed")[0]
        assert started["logging_schema_version"] == "1.0"
        assert completed["logging_schema_version"] == "1.0"


class TestForbiddenHeaderAllowlistFloor:
    """Art. 52: the middleware MUST NEVER log ``Authorization`` / ``Cookie`` /
    etc. even if a consumer names them in the allowlist. A hard-coded
    denylist filters the allowlist at startup.
    """

    def test_authorization_is_dropped_from_allowlist(self, rf, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=(
                "X-Tenant-ID",
                "Authorization",  # forbidden — MUST be filtered out.
                "Cookie",  # forbidden.
            ),
        )
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(
            rf.get(
                "/",
                headers={
                    "X-Tenant-ID": "tenant-42",
                    "Authorization": "Bearer leak-me",
                    "Cookie": "sessionid=leak-me",
                },
            )
        )
        completed = logger.events_for("request_completed")[0]
        # Safe header came through.
        assert completed["http.request.header.x_tenant_id"] == "tenant-42"
        # Forbidden headers NEVER appear, even under the allowlist prefix.
        assert "http.request.header.authorization" not in completed
        assert "http.request.header.cookie" not in completed

    def test_rejection_warning_emitted_at_startup(self, logger, settings_sandbox):
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=("Authorization", "X-OK"),
        )
        StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        rejected = logger.events_for("structured_logging_middleware_forbidden_headers_rejected")
        assert len(rejected) == 1
        assert "Authorization" in rejected[0]["rejected"]
        assert "X-OK" not in rejected[0]["rejected"]

    def test_case_insensitive_match(self, logger, settings_sandbox):
        """Forbidden names are matched case-insensitively so mixed-case
        mis-configurations can't slip through."""
        settings_sandbox(
            I_DOT_AI_LOGGER=logger,
            I_DOT_AI_LOGGING_HEADER_ALLOWLIST=("AUTHORIZATION", "CoOkIe"),
        )
        StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        rejected = logger.events_for("structured_logging_middleware_forbidden_headers_rejected")
        assert len(rejected) == 1
        # Both were rejected despite non-lowercase spelling.
        assert set(rejected[0]["rejected"]) == {"AUTHORIZATION", "CoOkIe"}


class TestUrlTruncation:
    """Art. 54: path capped at 2048, query at 1024, header values at 512."""

    def test_url_path_truncated(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        long_suffix = "x" * 4000
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(rf.get(f"/{long_suffix}"))
        completed = logger.events_for("request_completed")[0]
        assert len(completed["url.path"]) == 2048

    def test_url_query_truncated(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        long_query = "k=" + "v" * 4000
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(rf.get(f"/p?{long_query}"))
        completed = logger.events_for("request_completed")[0]
        assert len(completed["url.query"]) == 1024

    def test_user_agent_truncated(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        long_ua = "ua-" + ("z" * 1024)
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        middleware(rf.get("/", headers={"User-Agent": long_ua}))
        completed = logger.events_for("request_completed")[0]
        assert len(completed["user_agent.original"]) == 512


class TestDefaultLoggerDoesNotConfigureStructlog:
    """Art. 22: the middleware MUST NOT call ``structlog.configure()``,
    directly or transitively. Before this fix, the default logger path
    instantiated ``StructuredLogger()`` whose ``__init__`` calls
    ``ProcessorHelper().configure_processors(...)`` — a global structlog
    reconfiguration.
    """

    def test_instantiating_default_does_not_reconfigure_structlog(
        self,
        settings_sandbox,  # noqa: ARG002 - consumed via teardown only
    ):
        # Arrange: set up a known structlog config and snapshot it.
        structlog.reset_defaults()
        sentinel_processors = list(structlog.get_config()["processors"])

        # Ensure no I_DOT_AI_LOGGER is set — default path taken.
        if hasattr(django_settings, "I_DOT_AI_LOGGER"):
            delattr(django_settings, "I_DOT_AI_LOGGER")

        StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))

        # Structlog config MUST be unchanged.
        assert structlog.get_config()["processors"] == sentinel_processors

    def test_default_logger_is_callable_for_five_methods(
        self,
        rf,
        settings_sandbox,  # noqa: ARG002 - sandbox cleans settings  # noqa: COM819
    ):
        """Happy-path smoke: the default logger actually handles a request
        end-to-end without raising, even though it wraps a bare structlog
        bound logger rather than the library's ``StructuredLogger``.
        """
        if hasattr(django_settings, "I_DOT_AI_LOGGER"):
            delattr(django_settings, "I_DOT_AI_LOGGER")
        middleware = StructuredLoggingMiddleware(lambda _request: HttpResponse(status=200))
        # No logger injected; the default adapter should drive the request
        # without AttributeError on ``set_context_field`` / ``refresh_context``.
        result = middleware(rf.get("/x"))
        assert result is not None
        assert result.status_code == 200
