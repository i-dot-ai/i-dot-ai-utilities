"""Django ``StructuredLoggingMiddlewareOTel`` — the library's Django middleware.

Delegates HTTP-context extraction to OpenTelemetry's Django
auto-instrumentation: ``DjangoInstrumentor`` creates a server span per
request whose attributes carry ``http.request.method``, ``url.path``,
``http.route``, ``http.response.status_code`` etc.

The middleware keeps only the *log-lifecycle* concerns that cannot live
on a span:

- ``request_started`` / ``request_completed`` / ``request_failed`` events.
- ``duration_ms`` timing.
- Status-driven log-level selection (2xx→info, 4xx→warning, 5xx→error).
- ``request_id`` per-hop correlation (always a fresh UUID4 hex;
  verbatim inbound ``X-Request-ID`` is preserved separately as
  ``upstream_request_id`` when present and charset-valid).
- Exclusions (prefixes + regexes) — log-pipeline concern, not span concern.
- Header allowlist capture — log-pipeline concern, not span concern.
- ``exception.type`` / ``error.type`` on failures.
- ``Http404`` carve-out (WARNING + synthetic 404, never ERROR).
- Scope-ownership guard around ``refresh_context`` so a view calling
  ``logger.refresh_context()`` cannot silently de-correlate its own
  audit trail mid-request.

Trace correlation on log records comes from the
:func:`otel_trace_context_processor` structlog processor reading the
active span on every event — **not** from the middleware. Consumers MUST
call :func:`configure_otel_for_django` (or wire the processor into their
own structlog chain) for ``trace_id`` / ``span_id`` / ``trace_flags`` to
appear on log lines. The middleware emits a loud one-shot WARNING at
startup when it detects that no SDK ``TracerProvider`` has been
installed, so forgetting the setup step is visible immediately rather
than showing up as a silent absence of trace correlation.

``user.id`` is **not** extracted by this middleware. Consumers who need
authenticated-user attribution on log records should add
:class:`i_dot_ai_utilities.logging.middleware.django_user_id.DjangoUserIdMiddleware`
to ``settings.MIDDLEWARE`` after ``AuthenticationMiddleware`` and this
middleware. That middleware preserves the hardening (no DB query on
unhydrated ``SimpleLazyObject``, database-error WARNINGs) without
reintroducing HTTP-context extraction.

Sync-only, thread-safe under Gunicorn sync / gthread workers via
``structlog.contextvars``.
"""

from __future__ import annotations

import re
import sys
import time
import uuid
from typing import TYPE_CHECKING, ClassVar, Final

import structlog
from django.conf import settings  # type: ignore[import-untyped]
from django.core.exceptions import (  # type: ignore[import-untyped]
    ImproperlyConfigured,
    MiddlewareNotUsed,
)
from django.http import Http404  # type: ignore[import-untyped]

from i_dot_ai_utilities.logging.middleware._exclusions import (
    DEFAULT_EXCLUDED_PREFIXES,
    ExclusionMatcher,
)
from i_dot_ai_utilities.logging.middleware._headers import (
    FORBIDDEN_HEADER_NAMES,
    MAX_HEADER_VALUE,
    X_REQUEST_ID,
    validate_request_id,
)
from i_dot_ai_utilities.logging.middleware._levels import (
    LEVEL_ERROR,
    LEVEL_WARNING,
    duration_ms,
    level_for_status,
    truncate,
)
from i_dot_ai_utilities.logging.middleware._settings import (
    SETTING_ENABLED,
    SETTING_EXCLUDED_PREFIXES,
    SETTING_EXCLUDED_REGEXES,
    SETTING_HEADER_ALLOWLIST,
    SETTING_LOGGER,
    resolve_logger,
)
from i_dot_ai_utilities.logging.structured_logger import (
    REQUEST_SCOPE_OWNER_MIDDLEWARE,
    _claim_request_scope,
    _release_request_scope,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponse  # type: ignore[import-untyped]


# Event names kept as module constants so renaming is a single-file change.
_EVENT_STARTED = "request_started"
_EVENT_COMPLETED = "request_completed"
_EVENT_FAILED = "request_failed"
_EVENT_STARTUP = "structured_logging_middleware_otel_active"
_EVENT_FORBIDDEN_HEADERS_REJECTED = (
    "structured_logging_middleware_otel_forbidden_headers_rejected"
)
_EVENT_TRACER_PROVIDER_MISSING = (
    "structured_logging_middleware_otel_tracer_provider_missing"
)

# Synthesised statuses when a view raises.
_STATUS_SYNTH_ON_EXCEPTION: Final[int] = 500
_STATUS_NOT_FOUND: Final[int] = 404

# HTTP error threshold — 4xx/5xx responses carry ``error.type`` per OTel
# HTTP server semconv note 4 ("if status indicates an error... set to the
# status code number (represented as a string)").
_STATUS_ERROR_THRESHOLD: Final[int] = 400

# Schema version — bound onto every log event emitted by this middleware so
# downstream consumers can detect breaking changes without grepping source.
# Distinct from the original middleware's schema; start at 1.0 for the
# narrower OTel-backed log shape.
_SCHEMA_VERSION: Final[str] = "1.0"


class StructuredLoggingMiddlewareOTel:
    """Per-request structured-logging middleware for Django, OTel edition.

    Sync-only. Place high in ``MIDDLEWARE`` (after ``SecurityMiddleware``
    and ``AuthenticationMiddleware``; before view-level middleware) so
    the timer and lifecycle logs wrap the full request/response cycle.
    Django auto-instrumentation is configured at process startup via
    :func:`configure_otel_for_django`, independent of middleware ordering.
    """

    sync_capable: ClassVar[bool] = True
    async_capable: ClassVar[bool] = False

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self._get_response = get_response

        enabled = getattr(settings, SETTING_ENABLED, True)
        if not enabled:
            msg = "StructuredLoggingMiddlewareOTel disabled via settings."
            raise MiddlewareNotUsed(msg)

        self._logger = resolve_logger(getattr(settings, SETTING_LOGGER, None))

        prefixes_raw = getattr(settings, SETTING_EXCLUDED_PREFIXES, DEFAULT_EXCLUDED_PREFIXES)
        regexes_raw = getattr(settings, SETTING_EXCLUDED_REGEXES, ())
        self._exclusions = self._build_exclusions(prefixes_raw, regexes_raw)

        raw_allowlist = getattr(settings, SETTING_HEADER_ALLOWLIST, ())
        self._header_allowlist, rejected = self._normalise_header_allowlist(raw_allowlist)

        # (Art. 6, 51) One startup line so operators can see the middleware
        # is active; bind the schema version so the version is on the event
        # regardless of whether the consumer also inspects per-request logs.
        # A broken logger must not crash the worker; fall back to stderr so
        # the problem remains visible.
        self._emit_startup_log(prefixes_raw, regexes_raw)

        if rejected:
            self._emit_forbidden_headers_warning(rejected)

        # Loudly complain if the caller has not wired OpenTelemetry up at
        # process startup. Without a real SDK TracerProvider no spans are
        # produced and the ``otel_trace_context_processor`` has nothing to
        # inject — the middleware still works but trace correlation is
        # silently absent. The warning names the remediation so operators
        # don't have to grep docs to recover.
        self._warn_if_tracer_provider_missing()

    @staticmethod
    def _build_exclusions(
        prefixes_raw: object,
        regexes_raw: object,
    ) -> ExclusionMatcher:
        """Construct the path-exclusion matcher, mapping malformed input
        to ``ImproperlyConfigured`` (Art. 15).
        """
        try:
            return ExclusionMatcher(
                prefixes=prefixes_raw,  # type: ignore[arg-type]
                regexes=regexes_raw,  # type: ignore[arg-type]
            )
        except TypeError as exc:
            msg = (
                f"i_dot_ai_utilities logging: {SETTING_EXCLUDED_PREFIXES} / "
                f"{SETTING_EXCLUDED_REGEXES} must be iterables of strings; "
                f"got {type(exc).__name__}: {exc}."
            )
            raise ImproperlyConfigured(msg) from exc
        except re.error as exc:
            msg = (
                f"i_dot_ai_utilities logging: {SETTING_EXCLUDED_REGEXES} "
                f"contains an invalid regex: {exc}."
            )
            raise ImproperlyConfigured(msg) from exc

    @staticmethod
    def _normalise_header_allowlist(
        raw_allowlist: object,
    ) -> tuple[tuple[str, ...], list[str]]:
        """Apply the denylist floor to the header allowlist (Art. 52, 53).

        Returns ``(filtered_allowlist, rejected_names)``. Non-string entries
        are dropped silently (copy-paste bugs shouldn't brick startup).
        Malformed (non-iterable) settings raise ``ImproperlyConfigured``.
        """
        try:
            allowlist_iter = list(raw_allowlist)  # type: ignore[call-overload]
        except TypeError as exc:
            msg = (
                f"i_dot_ai_utilities logging: {SETTING_HEADER_ALLOWLIST} "
                f"must be an iterable of strings; got "
                f"{type(raw_allowlist).__name__}."
            )
            raise ImproperlyConfigured(msg) from exc

        filtered: list[str] = []
        rejected: list[str] = []
        for name in allowlist_iter:
            if not isinstance(name, str):
                continue
            if name.lower() in FORBIDDEN_HEADER_NAMES:
                rejected.append(name)
            else:
                filtered.append(name)
        return tuple(filtered), rejected

    def _emit_startup_log(
        self,
        prefixes_raw: object,
        regexes_raw: object,
    ) -> None:
        try:
            self._logger.info(
                _EVENT_STARTUP,
                logger=type(self._logger).__name__,
                excluded_prefixes=list(prefixes_raw),  # type: ignore[call-overload]
                excluded_regex_count=len(tuple(regexes_raw)),  # type: ignore[arg-type]
                header_allowlist_size=len(self._header_allowlist),
                logging_schema_version=_SCHEMA_VERSION,
            )
        except Exception as exc:  # noqa: BLE001 - last-resort observability
            print(  # noqa: T201
                f"i_dot_ai_utilities: startup log emission failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    def _emit_forbidden_headers_warning(self, rejected: list[str]) -> None:
        try:
            self._logger.warning(
                _EVENT_FORBIDDEN_HEADERS_REJECTED,
                rejected=list(rejected),
                logging_schema_version=_SCHEMA_VERSION,
            )
        except Exception as exc:  # noqa: BLE001 - last-resort observability
            print(  # noqa: T201
                f"i_dot_ai_utilities: forbidden-header warning failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

    def _warn_if_tracer_provider_missing(self) -> None:
        """Emit a one-shot WARNING if no SDK ``TracerProvider`` is installed.

        OpenTelemetry's API exposes a default ``ProxyTracerProvider`` when
        no real provider has been registered. Any spans the middleware
        relies on (and any ``trace_id`` / ``span_id`` the structlog
        processor would otherwise inject) are no-ops in that state.

        We check for a genuine SDK provider by isinstance rather than
        identity so subclasses (including test providers and vendor
        providers) are all accepted. A mis-configured process therefore
        gets one loud, named event at worker startup pointing operators
        straight at ``configure_otel_for_django`` as the fix. The check
        is tolerant: anything unexpected during the probe (including
        import errors in exotic test environments) is swallowed so
        middleware init never fails because of observability scaffolding.
        """
        try:
            from opentelemetry import trace  # noqa: PLC0415
            from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415

            provider = trace.get_tracer_provider()
            if isinstance(provider, TracerProvider):
                return

            try:
                self._logger.warning(
                    _EVENT_TRACER_PROVIDER_MISSING,
                    actual_provider=type(provider).__name__,
                    logging_schema_version=_SCHEMA_VERSION,
                    remediation=(
                        "call i_dot_ai_utilities.logging._otel."
                        "configure_otel_for_django(service_name=...) at "
                        "process startup (wsgi.py / asgi.py / "
                        "AppConfig.ready())"
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - last-resort observability
                print(  # noqa: T201
                    (
                        f"i_dot_ai_utilities: tracer-provider-missing warning "
                        f"failed: {type(exc).__name__}: {exc}"
                    ),
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 - probe must never crash init
            print(  # noqa: T201
                (
                    f"i_dot_ai_utilities: tracer-provider probe failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                file=sys.stderr,
            )

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # (Art. 21) Clear ``structlog.contextvars`` FIRST — before header
        # parsing, before the timer starts, before anything that could
        # raise. Protects against residual context leaking from a prior
        # request on the same Gunicorn sync-worker thread. The clear
        # lives here (not inside ``refresh_context``) because we're about
        # to claim request-scope ownership below, and the ownership
        # guard would otherwise refuse to clear mid-call.
        structlog.contextvars.clear_contextvars()

        # Rebuild the log context BEFORE claiming request-scope ownership.
        # If we claimed ownership first, the guard in
        # ``StructuredLogger.refresh_context`` would treat our own
        # initiating call as a re-entrant intrusion and emit a spurious
        # warning on every request. No enricher is passed — HTTP context
        # lives on the OTel span created by ``DjangoInstrumentor``; the
        # log record carries only the request_id + lifecycle fields
        # bound below, plus whatever the trace-context processor injects
        # on each event.
        self._logger.refresh_context(scope="request")

        # Now claim request-scope ownership for the duration of this
        # request. Manual ``refresh_context()`` calls from views or helpers
        # inside this window will be downgraded to "apply enrichers without
        # clearing" so audit-trail correlation cannot be silently broken
        # mid-request.
        scope_token = _claim_request_scope(REQUEST_SCOPE_OWNER_MIDDLEWARE)
        try:
            # (Art. 51) Bind the schema version as soon as ownership is
            # established so it appears on every event emitted during
            # this request.
            self._logger.set_context_field("logging_schema_version", _SCHEMA_VERSION)

            # (Art. 48) Exclusions: skip logging entirely. Still call
            # through to the view so health probes work.
            if self._exclusions.matches(request.path):
                return self._get_response(request)

            # (Art. 30, 32) Per-hop correlation id.
            # - ``request_id`` is ALWAYS a freshly generated UUID4 hex
            #   (distinct from trace_id, which comes from the OTel span).
            # - Any valid inbound ``X-Request-ID`` is preserved verbatim
            #   as ``upstream_request_id``; charset-invalid or absent
            #   inbound values produce no ``upstream_request_id`` field.
            self._bind_request_ids(request)

            # (Art. 53, 54) Allowlisted header capture. Denylist floor is
            # enforced at ``__init__`` time; header values are length-
            # capped here.
            self._bind_allowlisted_headers(request)

            # (Art. 37) Start the timer AFTER refresh_context so context-
            # setup cost is not counted, but before we log
            # ``request_started`` so the two log events bracket the same
            # interval.
            start = time.monotonic()

            self._logger.info(_EVENT_STARTED)

            # (Art. 40, 47) ``try`` / ``except`` / ``finally`` with an
            # idempotency guard: the completion event is emitted from
            # ``finally`` when the exception branches did not already log
            # their own event, so every non-excluded request produces
            # exactly one started + one completed-or-failed pair regardless
            # of control flow.
            response: HttpResponse | None = None
            status_code: int | None = None
            emitted: bool = False
            try:
                response = self._get_response(request)
                status_code = getattr(response, "status_code", _STATUS_SYNTH_ON_EXCEPTION)
            except Http404 as exc:
                # (Art. 46) 404 is ordinary traffic; log at WARNING, never
                # ERROR, and synthesise status 404 rather than the generic
                # 500. ``error.type`` uses the status code string per OTel
                # HTTP server semconv guidance.
                self._emit_http404(start, exc)
                emitted = True
                raise
            except Exception as exc:
                # (Art. 40-44) Log inside the ``except`` block so
                # ``sys.exc_info`` captures the traceback. Synthesise
                # status 500, bind OTel-aligned ``error.type`` (FQN), then
                # re-raise with bare ``raise`` to preserve the traceback
                # and keep Django's error handlers functional.
                self._emit_failed(start, exc)
                emitted = True
                raise
            finally:
                if not emitted and status_code is not None:
                    # Success path and 3xx/4xx/5xx responses that did not
                    # raise: emit exactly one ``request_completed``. We
                    # use ``finally`` rather than ``else`` so the
                    # structural guarantee "one event per request" is
                    # enforced by the control flow, not by convention.
                    assert response is not None  # for type narrowing  # noqa: S101
                    self._emit_completed(start, status_code)

            # (Art. 3) Return the response object unchanged.
            return response
        finally:
            _release_request_scope(scope_token)

    def process_exception(
        self,
        request: HttpRequest,
        exception: BaseException,
    ) -> None:
        """No-op exception enrichment hook.

        ``__call__``'s ``try/except`` is the source of truth for exception
        logging. This hook exists purely to satisfy Django's middleware
        dispatch protocol; returning ``None`` signals "keep looking"
        without intercepting the exception.

        Security contract (finding A7, matches the original middleware):
        this method MUST NOT write to structlog's contextvars. Django
        invokes ``process_exception`` on the way back up the chain before
        ``__call__``'s ``except`` block runs, and if a later middleware
        handles the exception and returns a response, any mutations here
        would bleed into the ``request_completed`` log line AND persist
        until the next ``refresh_context`` on the same worker thread.
        Keep this method a true no-op; all enrichment lives in ``__call__``
        where the scope-ownership guard contains the writes.

        ``request`` and ``exception`` are accepted for signature parity
        with Django's middleware protocol; intentionally unused.
        """
        # Explicitly no return statement: None signals "keep looking".

    # --- emitters -----------------------------------------------------------

    def _emit_completed(self, start: float, status_code: int) -> None:
        elapsed = duration_ms(start, time.monotonic())
        self._logger.set_context_field("http.response.status_code", status_code)
        self._logger.set_context_field("duration_ms", elapsed)
        # (Art. 50 + OTel) ``error.type`` for non-2xx/3xx responses. Uses
        # the HTTP status code string per the HTTP semconv note 4.
        if status_code >= _STATUS_ERROR_THRESHOLD:
            self._logger.set_context_field("error.type", str(status_code))

        level = level_for_status(status_code)
        self._emit(level, _EVENT_COMPLETED)

    def _emit_http404(self, start: float, exc: Http404) -> None:
        """Log ``Http404`` at WARNING with synthetic status 404.

        Constitution Art. 46: ``Http404`` MUST be logged at INFO or WARNING,
        never ERROR. We pick WARNING so it mirrors the normal
        ``level_for_status(404)`` result and consumers see a single rule:
        "4xx → warning". Emits ``request_completed`` (not ``request_failed``)
        because a 404 is an ordinary outcome, not a server-side failure.
        """
        elapsed = duration_ms(start, time.monotonic())
        self._logger.set_context_field("http.response.status_code", _STATUS_NOT_FOUND)
        self._logger.set_context_field("duration_ms", elapsed)
        # Record the exception type without a traceback — Http404 is a
        # control-flow signal, not a crash. ``error.type`` uses the status
        # code string to match the non-exception 4xx path.
        self._logger.set_context_field("exception.type", type(exc).__name__)
        self._logger.set_context_field("error.type", str(_STATUS_NOT_FOUND))
        self._logger.warning(_EVENT_COMPLETED)

    def _emit_failed(self, start: float, exc: Exception) -> None:
        """Log an unhandled exception at ERROR and synthesise status 500.

        Must be called from inside the ``except`` block so ``logger.exception``
        picks up ``sys.exc_info`` correctly (Art. 42).
        """
        elapsed = duration_ms(start, time.monotonic())
        self._logger.set_context_field("exception.type", type(exc).__name__)
        self._logger.set_context_field(
            "http.response.status_code", _STATUS_SYNTH_ON_EXCEPTION
        )
        self._logger.set_context_field("duration_ms", elapsed)
        # (Art. 50 + OTel) ``error.type`` on the exception path uses the
        # fully-qualified class name per HTTP server semconv note 4
        # ("exception type (its fully-qualified class name, if applicable)").
        cls = type(exc)
        fqn = f"{cls.__module__}.{cls.__qualname__}"
        self._logger.set_context_field("error.type", fqn)
        self._logger.exception(_EVENT_FAILED)

    # --- private helpers ----------------------------------------------------

    def _bind_request_ids(self, request: HttpRequest) -> None:
        """Bind ``request_id`` (always fresh) and optional ``upstream_request_id``.

        Constitution Art. 32: ``request_id`` MUST be a fresh per-hop UUID4,
        distinct from any inbound correlation value. Art. 30: inbound
        ``X-Request-ID`` is accepted verbatim, length-capped, and charset-
        restricted (security finding A3 — block log-injection / log-search
        hijack via attacker-chosen identifiers). When validation rejects an
        inbound value, ``upstream_request_id`` is simply omitted — the
        locally-minted ``request_id`` is unaffected.
        """
        self._logger.set_context_field("request_id", uuid.uuid4().hex)

        raw = request.headers.get(X_REQUEST_ID)
        validated = validate_request_id(raw) if raw is not None else None
        if validated is not None:
            self._logger.set_context_field("upstream_request_id", validated)

    def _bind_allowlisted_headers(self, request: HttpRequest) -> None:
        if not self._header_allowlist:
            return
        for header_name in self._header_allowlist:
            raw = request.headers.get(header_name)
            if raw is None:
                continue
            value = truncate(raw, MAX_HEADER_VALUE)
            normalised = header_name.lower().replace("-", "_")
            self._logger.set_context_field(
                f"http.request.header.{normalised}", value or ""
            )

    def _emit(self, level: str, event: str) -> None:
        """Dispatch to the correct logger method based on computed level."""
        if level == LEVEL_ERROR:
            self._logger.error(event)
        elif level == LEVEL_WARNING:
            self._logger.warning(event)
        else:
            self._logger.info(event)


__all__ = ["StructuredLoggingMiddlewareOTel"]
