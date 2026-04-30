"""Django ``StructuredLoggingMiddleware``.

Drop-in replacement for per-view ``refresh_context()`` calls. Runs once per
HTTP request and:

1. Clears and rebuilds structured-log context via ``refresh_context()`` with
   the ``DjangoEnricher`` — always the first statement of ``__call__``.
2. Parses inbound correlation headers (``traceparent``, ``X-Amzn-Trace-Id``,
   ``X-Request-ID``) and binds normalised ``trace_id`` / ``span_id`` /
   ``request_id`` onto the context, per a fixed precedence ladder.
3. Emits a ``request_started`` event, calls through the view, then emits
   either ``request_completed`` (success) or ``request_failed`` (exception),
   with a level selected from the HTTP status (``info`` / ``warning`` /
   ``error``).
4. Times the handler with ``time.monotonic()``; reports ``duration_ms`` as an
   integer.
5. On exception: logs with full traceback via ``logger.exception`` inside the
   ``except`` block, then re-raises with a bare ``raise`` so Django's error
   machinery (Sentry, DRF, debug toolbar, etc.) is untouched. ``Http404`` is
   carved out to a WARNING / 404 path per constitution Art. 46.
6. Supports health-check exclusions so high-volume probe endpoints do not
   fill the log store.

Thread-safe under Gunicorn sync workers via ``structlog.contextvars``. Sync-only
by design: ``sync_capable = True``, ``async_capable = False``.

Configuration
-------------

All settings are read lazily in ``__init__``. Defaults are chosen so the
middleware works without any configuration at all.

- ``I_DOT_AI_LOGGER`` (default: bare ``structlog.get_logger(__name__)``
  wrapped in an adapter) — logger object or zero-arg callable. Dotted
  strings are rejected (security finding A4).
- ``I_DOT_AI_LOGGING_MIDDLEWARE_ENABLED`` (default: ``True``) — set to
  ``False`` to disable the middleware cleanly via ``MiddlewareNotUsed``.
- ``I_DOT_AI_LOGGING_EXCLUDED_PREFIXES`` (default: health-check prefixes) —
  iterable of path prefixes to skip entirely.
- ``I_DOT_AI_LOGGING_EXCLUDED_REGEXES`` (default: empty) — iterable of regex
  strings, compiled once.
- ``I_DOT_AI_LOGGING_HEADER_ALLOWLIST`` (default: empty) — header names whose
  values should be bound to the log context (truncated to 512 chars). A
  hard-coded denylist (``FORBIDDEN_HEADER_NAMES``) is always applied on top
  of the allowlist so ``Authorization`` / ``Cookie`` / etc. cannot leak even
  if mis-configured (constitution Art. 52).
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING, ClassVar, Final

import structlog
from django.conf import settings  # type: ignore[import-untyped]
from django.core.exceptions import MiddlewareNotUsed  # type: ignore[import-untyped]
from django.http import Http404  # type: ignore[import-untyped]

from i_dot_ai_utilities.logging._limits import MAX_HEADER_VALUE, truncate
from i_dot_ai_utilities.logging.enrichers.enrichment_provider import (
    ContextEnrichmentType,
)
from i_dot_ai_utilities.logging.middleware._exclusions import (
    DEFAULT_EXCLUDED_PREFIXES,
    ExclusionMatcher,
)
from i_dot_ai_utilities.logging.middleware._headers import (
    FORBIDDEN_HEADER_NAMES,
    TRACEPARENT,
    X_AMZN_TRACE_ID,
    X_REQUEST_ID,
)
from i_dot_ai_utilities.logging.middleware._levels import (
    LEVEL_ERROR,
    duration_ms,
    level_for_status,
)
from i_dot_ai_utilities.logging.middleware._settings import (
    SETTING_ENABLED,
    SETTING_EXCLUDED_PREFIXES,
    SETTING_EXCLUDED_REGEXES,
    SETTING_HEADER_ALLOWLIST,
    SETTING_LOGGER,
    resolve_logger,
)
from i_dot_ai_utilities.logging.middleware._trace import resolve_trace_context
from i_dot_ai_utilities.logging.structured_logger import (
    REQUEST_SCOPE_OWNER_MIDDLEWARE,
    _claim_request_scope,
    _release_request_scope,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponse  # type: ignore[import-untyped]


# Event names — kept as module constants so renaming is a single-file change.
_EVENT_STARTED = "request_started"
_EVENT_COMPLETED = "request_completed"
_EVENT_FAILED = "request_failed"
_EVENT_STARTUP = "structured_logging_middleware_active"
_EVENT_FORBIDDEN_HEADERS_REJECTED = "structured_logging_middleware_forbidden_headers_rejected"

# Synthesised status when a view raises a non-Http404 exception.
_STATUS_SYNTH_ON_EXCEPTION: Final[int] = 500
_STATUS_NOT_FOUND: Final[int] = 404

# Schema version — bound onto every log event emitted by the middleware so
# downstream consumers can detect breaking changes without grepping source.
# Bump on ANY breaking change to field names, types, or semantics.
_SCHEMA_VERSION: Final[str] = "1.0"


class StructuredLoggingMiddleware:
    """Per-request structured-logging middleware for Django.

    Sync-only. Place high in ``MIDDLEWARE`` (after ``SecurityMiddleware`` and
    ``AuthenticationMiddleware``; before your view-level middleware) so the
    timer and logs wrap the full request/response cycle.
    """

    # Django async-adaptation flags.
    sync_capable: ClassVar[bool] = True
    async_capable: ClassVar[bool] = False

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self._get_response = get_response

        enabled = getattr(settings, SETTING_ENABLED, True)
        if not enabled:
            # Raised from __init__ so Django drops us from the chain cleanly.
            msg = "StructuredLoggingMiddleware disabled via settings."
            raise MiddlewareNotUsed(msg)

        self._logger = resolve_logger(getattr(settings, SETTING_LOGGER, None))

        prefixes = getattr(settings, SETTING_EXCLUDED_PREFIXES, DEFAULT_EXCLUDED_PREFIXES)
        regexes = getattr(settings, SETTING_EXCLUDED_REGEXES, ())
        self._exclusions = ExclusionMatcher(prefixes=prefixes, regexes=regexes)

        # Header allowlist: apply the hard-coded denylist floor before
        # storing. Rejected names are logged at WARNING so a misconfiguration
        # is visible immediately rather than silently allowed through.
        raw_allowlist = getattr(settings, SETTING_HEADER_ALLOWLIST, ())
        filtered: list[str] = []
        rejected: list[str] = []
        for name in raw_allowlist:
            if not isinstance(name, str):
                # Non-string entries in the allowlist are a typo / copy-paste
                # bug; drop them silently rather than breaking startup.
                continue
            if name.lower() in FORBIDDEN_HEADER_NAMES:
                rejected.append(name)
            else:
                filtered.append(name)
        self._header_allowlist: tuple[str, ...] = tuple(filtered)

        # One startup line so operators can see the middleware is active.
        # A broken logger must not crash the worker; fall back to stderr so
        # the problem remains visible.
        try:
            self._logger.info(
                _EVENT_STARTUP,
                logger=type(self._logger).__name__,
                excluded_prefixes=list(prefixes),
                excluded_regex_count=len(tuple(regexes)),
                header_allowlist_size=len(self._header_allowlist),
                logging_schema_version=_SCHEMA_VERSION,
            )
        except Exception as exc:  # noqa: BLE001 - last-resort observability
            print(  # noqa: T201
                f"i_dot_ai_utilities: startup log emission failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )

        if rejected:
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

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # (Art. 21) Clear ``structlog.contextvars`` FIRST. We do this
        # unconditionally, before anything else, so residual context from
        # a prior request on the same Gunicorn sync-worker thread cannot
        # bleed into this one. The clear lives here (not in
        # ``StructuredLogger.refresh_context``) because we're about to
        # claim request-scope ownership, and that ownership check would
        # otherwise refuse to clear mid-call.
        structlog.contextvars.clear_contextvars()

        # Rebuild the log context by invoking the Django enricher BEFORE
        # claiming request-scope ownership. If we claimed ownership first,
        # the guard in ``StructuredLogger.refresh_context`` would treat our
        # own initiating call as a re-entrant intrusion and emit a spurious
        # warning on every request (the owner sentinel would already be
        # "middleware" when the guard checks it). Refreshing first, then
        # claiming, keeps the guard silent for our own call while still
        # catching view/helper re-entry inside the ``try`` block below.
        self._logger.refresh_context(
            context_enrichers=[
                {"type": ContextEnrichmentType.DJANGO, "object": request},
            ],
            scope="request",
        )

        # Now claim request-scope ownership for the duration of this
        # request. Manual ``refresh_context()`` calls from views or helpers
        # inside this window will be downgraded to "apply enrichers without
        # clearing" so audit-trail correlation cannot be silently broken
        # mid-request (security finding: residual flaw).
        scope_token = _claim_request_scope(REQUEST_SCOPE_OWNER_MIDDLEWARE)
        try:
            # Bind the schema version as soon as ownership is established so
            # it appears on every event emitted during this request.
            self._logger.set_context_field("logging_schema_version", _SCHEMA_VERSION)

            # (Art. 48) Exclusions: skip logging entirely. Still call through
            # to the view so health probes work.
            if self._exclusions.matches(request.path):
                return self._get_response(request)

            # (Art. 26, 31-34) Read trace headers via the case-insensitive
            # ``request.headers`` API. Parse and bind.
            trace_context = resolve_trace_context(
                traceparent=request.headers.get(TRACEPARENT),
                amzn=request.headers.get(X_AMZN_TRACE_ID),
                req_id=request.headers.get(X_REQUEST_ID),
            )
            for key, value in trace_context.items():
                self._logger.set_context_field(key, value)

            # (Art. 53, 54) Optional allowlisted header capture. Request-level
            # OTel fields (``http.request.method``, ``url.path``, etc.) are
            # bound by the ``DjangoEnricher`` via ``refresh_context`` above;
            # the middleware is responsible only for lifecycle, trace
            # propagation, and arbitrary custom-header capture.
            self._bind_allowlisted_headers(request)

            # (Art. 37) Start the timer AFTER refresh_context so context-setup
            # cost is not counted, but before we log ``request_started`` so
            # the two log events bracket the same interval.
            start = time.monotonic()

            self._logger.info(_EVENT_STARTED)

            # (Art. 40) ``try`` / ``except`` / ``finally`` with an idempotency
            # guard: the completion event is emitted from ``finally`` when
            # the exception branches did not already log their own event, so
            # every non-excluded request produces exactly one started + one
            # completed-or-failed pair (Art. 47) regardless of control flow.
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
                self._emit_http404(request, start, exc)
                emitted = True
                raise
            except Exception as exc:
                # (Art. 40-44) Log inside the ``except`` block so
                # ``sys.exc_info`` captures the traceback. Synthesise status
                # 500, bind OTel-aligned ``error.type`` (FQN), then re-raise
                # with bare ``raise`` to preserve the traceback and keep
                # Django's error handlers functional.
                self._emit_failed(request, start, exc)
                emitted = True
                raise
            finally:
                if not emitted and status_code is not None:
                    # Success path and 3xx/4xx/5xx responses that did not
                    # raise: emit exactly one ``request_completed``. We use
                    # ``finally`` rather than ``else`` so the structural
                    # guarantee "one event per request" is enforced by the
                    # control flow, not by convention.
                    assert response is not None  # for type narrowing  # noqa: S101
                    self._emit_completed(request, start, response, status_code)

            # (Art. 3) Return the response object unchanged.
            return response
        finally:
            _release_request_scope(scope_token)

    def process_exception(
        self,
        request: HttpRequest,
        exception: BaseException,
    ) -> None:
        """Best-effort enrichment hook. Never raises, never returns a response.

        ``__call__``'s ``try/except`` is the source of truth for exception
        logging and route binding. This hook exists purely to satisfy Django's
        middleware dispatch protocol; returning ``None`` signals "keep looking"
        without intercepting the exception.

        Security note (finding A7): this method MUST NOT write to structlog's
        contextvars. Django invokes ``process_exception`` on the way back up
        the chain before ``__call__``'s ``except`` block runs, and if a later
        middleware handles the exception and returns a response, any mutations
        here would bleed into the ``request_completed`` log line AND persist
        until the next ``refresh_context`` on the same worker thread. Keep
        this method a true no-op; all enrichment lives in ``__call__`` where
        the scope-ownership guard contains the writes.
        """
        # Explicitly no return statement: None signals "keep looking".

    # --- emitters -----------------------------------------------------------

    def _emit_completed(
        self,
        request: HttpRequest,
        start: float,
        _response: HttpResponse,
        status_code: int,
    ) -> None:
        elapsed = duration_ms(start, time.monotonic())
        # URL routing runs between ``refresh_context`` (where the enricher
        # ran pre-routing) and here, so re-read ``resolver_match`` now to
        # pick up the matched view for the completion event.
        self._bind_route_if_resolved(request)
        self._logger.set_context_field("http.response.status_code", status_code)
        self._logger.set_context_field("duration_ms", elapsed)
        # (Art. 50 + OTel) ``error.type`` for non-2xx/3xx responses. Uses
        # the HTTP status code string per the HTTP semconv note 4: "if
        # status indicates an error ... set to the status code number
        # (represented as a string)".
        if status_code >= 400:  # noqa: PLR2004 - matches HTTP error threshold
            self._logger.set_context_field("error.type", str(status_code))

        level = level_for_status(status_code)
        self._emit(level, _EVENT_COMPLETED)

    def _emit_http404(
        self,
        request: HttpRequest,
        start: float,
        exc: Http404,
    ) -> None:
        """Log ``Http404`` at WARNING with synthetic status 404.

        Constitution Art. 46: ``Http404`` MUST be logged at INFO or WARNING,
        never ERROR. We pick WARNING so it mirrors the normal
        ``level_for_status(404)`` result and consumers see a single rule:
        "4xx → warning". Emits ``request_completed`` (not ``request_failed``)
        because a 404 is an ordinary outcome, not a server-side failure.
        """
        elapsed = duration_ms(start, time.monotonic())
        self._bind_route_if_resolved(request)
        self._logger.set_context_field("http.response.status_code", _STATUS_NOT_FOUND)
        self._logger.set_context_field("duration_ms", elapsed)
        # Record the exception type without a traceback — Http404 is a
        # control-flow signal, not a crash. FQN aligns with OTel HTTP
        # semconv on ``error.type``; the simple ``.__name__`` is kept on
        # ``exception.type`` for ergonomics (matches the non-Http404 path).
        self._logger.set_context_field("exception.type", type(exc).__name__)
        self._logger.set_context_field("error.type", str(_STATUS_NOT_FOUND))
        self._logger.warning(_EVENT_COMPLETED)

    def _emit_failed(
        self,
        request: HttpRequest,
        start: float,
        exc: Exception,
    ) -> None:
        """Log an unhandled exception at ERROR and synthesise status 500.

        Must be called from inside the ``except`` block so ``logger.exception``
        picks up ``sys.exc_info`` correctly (Art. 42).
        """
        elapsed = duration_ms(start, time.monotonic())
        self._bind_route_if_resolved(request)
        self._logger.set_context_field("exception.type", type(exc).__name__)
        self._logger.set_context_field("http.response.status_code", _STATUS_SYNTH_ON_EXCEPTION)
        self._logger.set_context_field("duration_ms", elapsed)
        # (Art. 50 + OTel) ``error.type`` on the exception path uses the
        # fully-qualified class name per HTTP server semconv note 4
        # ("exception type (its fully-qualified class name, if applicable)").
        cls = type(exc)
        fqn = f"{cls.__module__}.{cls.__qualname__}"
        self._logger.set_context_field("error.type", fqn)
        self._logger.exception(_EVENT_FAILED)

    # --- private helpers ----------------------------------------------------

    def _bind_route_if_resolved(self, request: HttpRequest) -> None:
        """Bind ``http.route`` / ``django.url_name`` if URL routing has run.

        The ``DjangoEnricher`` runs inside ``refresh_context`` at the very top
        of ``__call__``, which is before Django's URL resolver executes. By
        the time the response is ready (or an exception has escaped the view)
        ``request.resolver_match`` is populated, so we re-read it here to
        upgrade the two route fields.

        Swallows any error — an observability helper must never take the
        request down because its own enrichment raised.
        """
        try:
            resolver_match = getattr(request, "resolver_match", None)
            if resolver_match is None:
                return
            view_name = getattr(resolver_match, "view_name", None)
            url_name = getattr(resolver_match, "url_name", None)
            if isinstance(view_name, str):
                self._logger.set_context_field("http.route", view_name)
            if isinstance(url_name, str):
                self._logger.set_context_field("django.url_name", url_name)
        except Exception:  # noqa: BLE001 - observability must not break the request
            return

    def _bind_allowlisted_headers(self, request: HttpRequest) -> None:
        if not self._header_allowlist:
            return
        for header_name in self._header_allowlist:
            raw = request.headers.get(header_name)
            if raw is None:
                continue
            value = truncate(raw, MAX_HEADER_VALUE)
            # Field names are intentionally flattened with a prefix so log
            # consumers can tell header-sourced fields apart.
            normalised = header_name.lower().replace("-", "_")
            self._logger.set_context_field(f"http.request.header.{normalised}", value or "")

    def _emit(self, level: str, event: str) -> None:
        """Dispatch to the correct logger method based on computed level."""
        if level == LEVEL_ERROR:
            self._logger.error(event)
        elif level == "warning":
            self._logger.warning(event)
        else:
            self._logger.info(event)
