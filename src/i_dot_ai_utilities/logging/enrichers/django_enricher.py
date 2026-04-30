from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any

from i_dot_ai_utilities.logging._limits import (
    MAX_HEADER_VALUE,
    MAX_URL_PATH,
    MAX_URL_QUERY,
    truncate,
)

if TYPE_CHECKING:
    from i_dot_ai_utilities.logging.types.django_enrichment_schema import (
        DjangoRequestLike,
        ExtractedDjangoContext,
    )

# Django exception class names we want to surface distinctly if the user
# object triggers a database-layer failure during attribute access. Matched
# by class name so the enricher module avoids importing Django at module
# load time (see ``test_optional_django_import.py`` — the "no top-level
# Django imports outside middleware/django.py" invariant).
_DB_ERROR_CLASS_NAMES = frozenset(
    {
        "DatabaseError",
        "OperationalError",
        "InterfaceError",
        "ProgrammingError",
        "IntegrityError",
    }
)


# Explicit attribute list used by the fallback duck-typed validation when
# Django is not importable. Spelled out here rather than via the Protocol's
# ``runtime_checkable`` machinery so the check is harder to spoof — e.g. an
# object with attributes named but overridden by __getattr__ side effects
# is still rejected if any single attribute is missing.
_REQUIRED_REQUEST_ATTRS: tuple[str, ...] = (
    "method",
    "path",
    "scheme",
    "META",
    "GET",
    "headers",
    "get_host",
)


# Cached ``django.http.HttpRequest`` reference for strict isinstance checks.
# Populated lazily on first use so the enricher module stays importable in
# non-Django consumers. ``False`` sentinel means "Django is unavailable in
# this environment" (distinct from ``None`` which means "not looked up yet").
_HTTP_REQUEST_CLS: Any = None


def _load_http_request_cls() -> Any:
    """Return ``django.http.HttpRequest`` if importable, else ``False``.

    Cached after the first call. The negative sentinel (``False``) is
    explicit so repeated misses don't re-attempt the import.
    """
    global _HTTP_REQUEST_CLS  # noqa: PLW0603
    if _HTTP_REQUEST_CLS is not None:
        return _HTTP_REQUEST_CLS
    try:
        from django.http import HttpRequest  # type: ignore[import-untyped]  # noqa: PLC0415
    except ImportError:
        _HTTP_REQUEST_CLS = False
        return False
    _HTTP_REQUEST_CLS = HttpRequest
    return HttpRequest


def _is_database_error(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like a Django database-layer exception.

    Walks the MRO by class name rather than isinstance so we don't have to
    import Django here. Django's database exception hierarchy has been stable
    since 1.x so the name-based check is adequate; if a real DB failure ever
    slips through, the outer ``except Exception`` still catches it — we just
    lose the dedicated WARNING and fall back to the generic "failed to
    extract Django fields" message.
    """
    return any(cls.__name__ in _DB_ERROR_CLASS_NAMES for cls in type(exc).__mro__)


def _looks_like_unhydrated_lazy_object(user: Any) -> bool:
    """Detect Django's ``SimpleLazyObject`` before it has hydrated.

    Touching ``.pk`` on an unhydrated ``SimpleLazyObject`` forces it to
    evaluate, which for ``request.user`` means issuing a database query to
    look up the user row. Observability code must never issue such a query
    — it can mask database outages (security finding FI-5) and alters
    query counts in ways that interfere with N+1 detection.

    We detect by class name + the documented ``_wrapped`` sentinel so we
    don't have to import Django. If the sentinel attribute is missing or
    the wrapped value is already populated (``!=`` ``empty``), we return
    False and the caller accesses ``.pk`` as normal.
    """
    if type(user).__name__ != "SimpleLazyObject":
        return False
    wrapped = getattr(user, "_wrapped", None)
    if wrapped is None:
        return True
    # Django's ``empty`` sentinel is a module-level object; we identify it
    # structurally by its class name to avoid the import.
    return type(wrapped).__name__ == "empty"


class DjangoEnricher:
    """Per-request enricher for Django ``HttpRequest`` objects.

    Emits a flat dict of OpenTelemetry semantic-convention field names
    (``http.request.method``, ``url.path``, ``client.address``, etc.). Fields
    with no source on the request are omitted entirely rather than emitted as
    ``None`` / empty-string, so log lines stay compact.

    Fields under the ``django.*`` namespace are framework-specific values with
    no OTel equivalent.

    Security notes:

    - Finding A1 (request type spoofing): the enricher prefers an
      ``isinstance(request, django.http.HttpRequest)`` check when Django is
      importable. When Django is absent (non-Django consumer, unit test
      without Django, duck-typed surrogate), the check falls back to the
      ``DjangoRequestLike`` protocol and a warning is emitted to the
      provided logger once per enricher instance. Use ``strict=True``
      (the default) to enforce the isinstance check; ``strict=False``
      silences the warning for callers who explicitly opt into the
      duck-typed contract (e.g. library unit tests without Django).
    - Finding A8 (secrets in query strings): ``url.query`` contains the
      full raw query string. NEVER pass secrets, OAuth codes, tokens,
      session identifiers, API keys, or personally identifiable
      information via query parameters in services using this enricher.
      Common offenders that will leak verbatim to the log store include
      ``?code=``, ``?token=``, ``?access_token=``, ``?id_token=``,
      ``?refresh_token=``, ``?api_key=``, ``?state=``, ``?authorization=``,
      ``?password=``, ``?secret=``. Query-string redaction is the
      consumer's responsibility; this library deliberately does not
      silently rewrite URLs to preserve debuggability.
    """

    def __init__(self, strict: bool = True) -> None:
        self._strict = strict
        # Track whether we've already emitted the fallback warning so each
        # enricher instance nags the operator at most once — noisy warnings
        # erode trust and get ignored.
        self._fallback_warned = False

    def extract_context(self, logger: Any, request: DjangoRequestLike) -> ExtractedDjangoContext | None:
        response: ExtractedDjangoContext | None = None
        try:
            self._validate_object_instance(logger, request)

            # Always-present fields. Any exception raised here propagates to
            # the outer ``except`` and surfaces via ``logger.exception``.
            # ``url.path`` / ``url.query`` are length-capped at ingest per
            # constitution Art. 54 so pathological requests cannot produce
            # multi-MB log lines.
            result: ExtractedDjangoContext = {
                "http.request.method": request.method or "",
                "url.scheme": request.scheme,
                "url.path": truncate(request.path, MAX_URL_PATH) or "",
                "url.query": truncate(self._extract_query(request), MAX_URL_QUERY) or "",
                "server.address": request.get_host(),
            }

            # Conditionally-present fields: only emit when a real value exists.
            user_agent = self._get_header(request, "user-agent")
            if user_agent is not None:
                result["user_agent.original"] = truncate(user_agent, MAX_HEADER_VALUE) or ""

            client_address = self._get_client_address(request)
            if client_address is not None:
                result["client.address"] = client_address

            xff = self._get_header(request, "x-forwarded-for")
            if xff is not None:
                result["http.request.header.x_forwarded_for"] = truncate(xff, MAX_HEADER_VALUE) or ""

            http_route = self._safe_resolver_attr(request, "view_name")
            if http_route is not None:
                result["http.route"] = http_route

            url_name = self._safe_resolver_attr(request, "url_name")
            if url_name is not None:
                result["django.url_name"] = url_name

            # (Art. 55 + OTel alignment) User identity is emitted as
            # ``user.id`` only — ``enduser.*`` was deprecated upstream in
            # OTel 1.24, and the constitution forbids email/username/PII.
            user_id = self._extract_user(logger, request)
            if user_id is not None:
                result["user.id"] = user_id

            response = result
        except Exception:
            logger.exception("Exception(Logger): Failed to extract Django fields")
            return None
        else:
            return response

    def _validate_object_instance(self, logger: Any, request: DjangoRequestLike) -> None:
        """Validate the request shape before extracting fields.

        Security (finding A1): prefer the Django ``isinstance`` check when
        available. Any object that merely exposes the right attribute names
        passes the ``DjangoRequestLike`` Protocol; an attacker-controlled
        middleware attaching a surrogate could otherwise cause forged user
        IDs / paths to be logged as if they came from the real request.

        When Django is not importable (non-Django consumer, unit test
        without Django), fall back to the Protocol check and emit a
        one-shot warning pointing at the risk. Callers who have legitimately
        opted out (``strict=False``) skip the warning silently.
        """
        http_request_cls = _load_http_request_cls()
        if http_request_cls is not False:
            # Django is available: the strict isinstance check is the
            # source of truth. Protocol conformance is not sufficient.
            if not isinstance(request, http_request_cls):
                msg = (
                    "Exception(Logger): Request object is not an instance "
                    "of django.http.HttpRequest. Context not set. (This is a "
                    "security-relevant check — see enricher docstring for "
                    "finding A1.)"
                )
                raise TypeError(msg)
            return

        # Django isn't importable here. Fall back to explicit attribute
        # presence checking. We deliberately avoid ``isinstance(request,
        # DjangoRequestLike)`` — the Protocol's ``runtime_checkable``
        # machinery only verifies attribute presence, which is the same
        # check, but via a machinery that encourages treating "conforms" as
        # "safe". Being explicit here keeps the fallback narrow.
        for attr in _REQUIRED_REQUEST_ATTRS:
            if not hasattr(request, attr):
                msg = "Exception(Logger): Request object doesn't conform to DjangoRequestLike. Context not set."
                raise TypeError(msg)

        if self._strict and not self._fallback_warned:
            self._fallback_warned = True
            # Logger must not break enrichment: if the warn call itself fails
            # (custom logger with unexpected signature, etc.) we silently
            # ignore it. The warning is nice-to-have; the isinstance check
            # above is the actual safety mechanism.
            with contextlib.suppress(Exception):
                logger.warning(
                    "Warning(Logger): DjangoEnricher validated a request via "
                    "the duck-typed Protocol because django.http.HttpRequest "
                    "is not importable. This path is weaker against type-"
                    "confusion attacks (finding A1). If this is a Django "
                    "service, ensure Django is installed. If this is a test "
                    "harness, construct DjangoEnricher(strict=False) to "
                    "silence this warning."
                )

    def _get_header(self, request: DjangoRequestLike, name: str) -> str | None:
        """Return a non-empty header value, or ``None`` if absent.

        Django >= 2.2 exposes ``request.headers`` as a case-insensitive
        mapping. We fall back to ``META`` for minimal duck-typed objects.
        """
        headers = getattr(request, "headers", None)
        if headers is not None:
            try:
                value = headers.get(name, None)
            except (AttributeError, TypeError):
                value = None
            if isinstance(value, str) and value:
                return value

        meta_key = f"HTTP_{name.upper().replace('-', '_')}"
        meta_value = request.META.get(meta_key, None)
        if isinstance(meta_value, str) and meta_value:
            return meta_value
        return None

    def _get_client_address(self, request: DjangoRequestLike) -> str | None:
        """Return ``REMOTE_ADDR`` as seen by the app, or ``None`` if absent.

        Note: this is the peer Django sees. Behind a load balancer it will be
        the proxy's address, not the true client. ``X-Forwarded-For`` is
        exposed separately as ``http.request.header.x_forwarded_for`` so
        operators can decide which to trust.
        """
        value = request.META.get("REMOTE_ADDR", None)
        if isinstance(value, str) and value:
            return value
        return None

    def _extract_query(self, request: DjangoRequestLike) -> str:
        # Django's ``QueryDict`` always exposes ``urlencode()``. We trust the
        # Protocol contract here rather than branching defensively.
        result = request.GET.urlencode()
        return result if isinstance(result, str) else ""

    def _safe_resolver_attr(self, request: DjangoRequestLike, attr: str) -> str | None:
        # ``resolver_match`` is None before URL resolution runs and during 404s,
        # so every access must be guarded.
        resolver_match = getattr(request, "resolver_match", None)
        if resolver_match is None:
            return None
        value = getattr(resolver_match, attr, None)
        return value if isinstance(value, str) else None

    def _extract_user(  # noqa: PLR0911 - explicit early returns aid auditability of the safety branches
        self, logger: Any, request: DjangoRequestLike
    ) -> str | None:
        """Return the authenticated user's primary key as a string, or ``None``.

        Returns ``None`` for any of:

        - ``request.user`` is absent (auth middleware hasn't run).
        - The user is an unhydrated ``SimpleLazyObject`` (see FI-5 below).
        - ``is_authenticated`` is falsy or not a bool.
        - ``pk`` / ``id`` access raises a database-layer error.

        We never emit ``enduser.authenticated`` (OTel deprecated the
        ``enduser.*`` namespace in v1.24) and we never emit PII — only the
        opaque primary key. Anonymous users produce no ``user.id`` field at
        all rather than a falsy value, so downstream queries like
        ``has(user.id)`` discriminate cleanly.

        Security (finding FI-5): observability code must never issue a
        database query. ``is_authenticated`` is read FIRST; if the user is
        not authenticated we skip the ``pk`` / ``id`` lookup entirely.
        Unhydrated ``SimpleLazyObject`` instances are detected without
        forcing evaluation so extracting the user id never triggers the
        auth backend's ``User.objects.get()`` query path. Any remaining
        database-layer failure during attribute access is surfaced via a
        WARNING log rather than silently swallowed (which previously hid
        DB outages from the very logs that would have diagnosed them).
        """
        user = getattr(request, "user", None)
        if user is None:
            return None

        # Detect Django's ``SimpleLazyObject`` before forcing hydration.
        # Accessing attributes on an unhydrated lazy user triggers a DB
        # query via the auth backend — forbidden for an enricher.
        if _looks_like_unhydrated_lazy_object(user):
            return None

        # Read authentication state first. If the user is anonymous or the
        # attribute isn't a bool, don't touch ``pk`` at all.
        try:
            is_authenticated_attr = getattr(user, "is_authenticated", None)
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment
            if _is_database_error(exc):
                logger.warning(
                    "Warning(Logger): Database error reading request.user.is_authenticated; skipping user enrichment"
                )
                return None
            return None

        if is_authenticated_attr is not True:
            return None

        try:
            user_id_attr = getattr(user, "pk", None)
            if user_id_attr is None:
                user_id_attr = getattr(user, "id", None)
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment
            if _is_database_error(exc):
                logger.warning(
                    "Warning(Logger): Database error reading request.user primary key; user.id omitted from log context"
                )
                return None
            return None

        return str(user_id_attr) if user_id_attr is not None else None
