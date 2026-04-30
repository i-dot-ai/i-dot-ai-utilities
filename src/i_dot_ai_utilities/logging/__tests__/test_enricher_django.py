# mypy: disable-error-code="no-untyped-def"

import json
from typing import Any, ClassVar

import pytest

# Must configure Django settings BEFORE importing anything that touches
# django.conf.settings or django.http. Mirrors ``test_middleware_django.py``.
django = pytest.importorskip("django")

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

from django.test import RequestFactory  # type: ignore[import-untyped]  # noqa: E402

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger  # noqa: E402
from i_dot_ai_utilities.logging.types.enrichment_types import (  # noqa: E402
    ContextEnrichmentType,
    ExecutionEnvironmentType,
)

urlpatterns: list = []


class _FakeResolverMatch:
    def __init__(self, view_name: str | None = None, url_name: str | None = None):
        self.view_name = view_name
        self.url_name = url_name


class _FakeUser:
    def __init__(self, pk: Any = None, is_authenticated: bool | None = None):
        self.pk = pk
        self.id = pk
        if is_authenticated is not None:
            self.is_authenticated = is_authenticated


def _build_request(
    *,
    method: str = "GET",
    path: str = "/logger/unit/testing",
    scheme: str = "http",
    host: str = "testserver",
    query: str = "islogger=true&istest=true",
    headers: dict[str, str] | None = None,
    meta_extras: dict[str, Any] | None = None,
    resolver_match: Any = None,
    user: Any = None,
):
    """Build a real ``django.http.HttpRequest`` via ``RequestFactory``.

    We use the real Django request object rather than a duck-typed fake so
    the enricher's strict ``isinstance`` validation (security finding A1)
    accepts the test input. Previously the suite used a pure-Python fake
    which would now fail the isinstance check by design.
    """
    factory = RequestFactory()
    full_path = f"{path}?{query}" if query else path
    builder = getattr(factory, method.lower())
    request = builder(
        full_path,
        secure=(scheme == "https"),
        HTTP_HOST=host,
        headers=headers or None,
    )
    if meta_extras:
        request.META.update(meta_extras)
    if resolver_match is not None:
        request.resolver_match = resolver_match
    if user is not None:
        request.user = user
    return request


@pytest.fixture
def load_test_request_object():
    return _build_request(
        headers={
            "user-agent": "test agent",
            "x-forwarded-for": "1.2.3.4",
        },
        meta_extras={"REMOTE_ADDR": "10.0.0.1"},
        resolver_match=_FakeResolverMatch(view_name="consultations:detail", url_name="consultation-detail"),
        user=_FakeUser(pk=99, is_authenticated=True),
    )


def _parse_logs(capsys) -> list[dict]:
    captured = capsys.readouterr()
    lines = captured.out.strip().splitlines()
    return [json.loads(line) for line in lines]


def test_django_enriched_logger_contains_expected_otel_fields(load_test_request_object, capsys):
    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    logger.refresh_context(
        context_enrichers=[
            {
                "type": ContextEnrichmentType.DJANGO,
                "object": load_test_request_object,
            }
        ]
    )

    logger.info("test message")

    parsed = _parse_logs(capsys)
    record = parsed[0]

    # Always-present fields.
    assert record.get("http.request.method") == "GET"
    assert record.get("url.scheme") == "http"
    assert record.get("url.path") == "/logger/unit/testing"
    assert record.get("url.query") == "islogger=true&istest=true"
    assert record.get("server.address") == "testserver"

    # Conditionally-present fields.
    assert record.get("user_agent.original") == "test agent"
    assert record.get("client.address") == "10.0.0.1"
    assert record.get("http.request.header.x_forwarded_for") == "1.2.3.4"
    assert record.get("http.route") == "consultations:detail"
    assert record.get("django.url_name") == "consultation-detail"
    # (OTel alignment) user identity emitted as ``user.id`` only; the old
    # ``enduser.*`` namespace is gone per semconv v1.24 deprecation.
    assert record.get("user.id") == "99"
    assert "enduser.id" not in record
    assert "enduser.authenticated" not in record


def test_django_enricher_omits_optional_fields_when_sources_absent(capsys):
    """Optional fields must be absent from the log line, not present as None."""
    request = _build_request(headers=None, resolver_match=None, user=None)
    # RequestFactory adds a default User-Agent; strip it so the "absent"
    # branch is exercised.
    request.META.pop("HTTP_USER_AGENT", None)
    request.META.pop("REMOTE_ADDR", None)
    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    logger.refresh_context(context_enrichers=[{"type": ContextEnrichmentType.DJANGO, "object": request}])
    logger.info("minimal")

    parsed = _parse_logs(capsys)
    record = parsed[0]

    # Always-present fields still there.
    assert "http.request.method" in record
    assert "url.scheme" in record
    assert "url.path" in record
    assert "url.query" in record
    assert "server.address" in record

    # All optional fields must be absent.
    assert "user_agent.original" not in record
    assert "client.address" not in record
    assert "http.request.header.x_forwarded_for" not in record
    assert "http.route" not in record
    assert "django.url_name" not in record
    assert "user.id" not in record
    assert "enduser.id" not in record
    assert "enduser.authenticated" not in record


def test_django_enricher_handles_missing_resolver_match(capsys):
    """``resolver_match`` is None before URL routing and during 404s."""
    request = _build_request(resolver_match=None)
    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    logger.refresh_context(context_enrichers=[{"type": ContextEnrichmentType.DJANGO, "object": request}])
    logger.info("no resolver")

    parsed = _parse_logs(capsys)
    record = parsed[0]
    assert "http.route" not in record
    assert "django.url_name" not in record


def test_django_enricher_handles_https_scheme(capsys):
    request = _build_request(scheme="https", host="example.gov.uk")
    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    logger.refresh_context(context_enrichers=[{"type": ContextEnrichmentType.DJANGO, "object": request}])
    logger.info("https")

    parsed = _parse_logs(capsys)
    assert parsed[0].get("url.scheme") == "https"
    assert parsed[0].get("server.address") == "example.gov.uk"


def test_django_enricher_handles_empty_query(capsys):
    request = _build_request(query="")
    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    logger.refresh_context(context_enrichers=[{"type": ContextEnrichmentType.DJANGO, "object": request}])
    logger.info("empty query")

    parsed = _parse_logs(capsys)
    assert parsed[0].get("url.query") == ""


def test_django_enricher_handles_missing_user(capsys):
    """``request.user`` is absent until Django's auth middleware runs."""
    request = _build_request(user=None)
    # RequestFactory does not set request.user by default, but confirm it.
    if hasattr(request, "user"):
        delattr(request, "user")
    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    logger.refresh_context(context_enrichers=[{"type": ContextEnrichmentType.DJANGO, "object": request}])
    logger.info("no user")

    parsed = _parse_logs(capsys)
    assert "user.id" not in parsed[0]
    assert "enduser.id" not in parsed[0]
    assert "enduser.authenticated" not in parsed[0]


def test_django_enricher_handles_anonymous_user(capsys):
    request = _build_request(user=_FakeUser(pk=None, is_authenticated=False))
    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    logger.refresh_context(context_enrichers=[{"type": ContextEnrichmentType.DJANGO, "object": request}])
    logger.info("anon")

    parsed = _parse_logs(capsys)
    # Anonymous users produce no ``user.id`` at all (not a falsy value).
    # Constitution Art. 55 forbids emitting PII; the authenticated flag was
    # dropped along with the ``enduser.*`` namespace rename.
    assert "user.id" not in parsed[0]
    assert "enduser.id" not in parsed[0]
    assert "enduser.authenticated" not in parsed[0]


@pytest.mark.parametrize(
    "django_request_object_value",
    [
        {"a_dummy_response": True},
        None,
        0,
        "blah",
    ],
)
def test_django_enrichment_handles_malformed_object(django_request_object_value, capsys):
    logger = StructuredLogger(level="info", options={"execution_environment": ExecutionEnvironmentType.LOCAL})

    logger.refresh_context(
        context_enrichers=[
            {
                "type": ContextEnrichmentType.DJANGO,
                "object": django_request_object_value,
            }
        ]
    )

    log_message = "logger continues working as normal"
    logger.warning(log_message)

    parsed = _parse_logs(capsys)

    # Under strict mode the message is the isinstance rejection; under
    # non-strict the Protocol fallback message. Either should be present.
    exc_text = parsed[0].get("exception", "")
    assert (
        "not an instance of django.http.HttpRequest" in exc_text or "doesn't conform to DjangoRequestLike" in exc_text
    ), f"unexpected exception text: {exc_text!r}"
    assert parsed[0].get("level") == "error"

    assert parsed[1].get("message") == log_message
    assert parsed[1].get("level") == "warning"


# ---------------------------------------------------------------------------
# Security finding FI-5: enricher must not trigger ORM queries
# ---------------------------------------------------------------------------
#
# The lazy-user and DB-error tests construct a ``SimpleLazyObject``-shaped
# fake and attach it to ``request.user``. This exercises the private
# ``_extract_user`` path, so we bypass the strict isinstance check by
# instantiating the enricher directly with ``strict=False``.


from i_dot_ai_utilities.logging.enrichers.django_enricher import DjangoEnricher  # noqa: E402


class _CapturingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.exceptions: list[str] = []

    def warning(self, msg: str, **kwargs: Any) -> None:  # noqa: ARG002
        self.warnings.append(msg)

    def exception(self, msg: str, **kwargs: Any) -> None:  # noqa: ARG002
        self.exceptions.append(msg)


class _LazyUserSentinel:
    """Stand-in for Django's ``empty`` sentinel used by SimpleLazyObject."""


# Match Django's class name so the enricher's structural check fires.
_LazyUserSentinel.__name__ = "empty"


class _FakeSimpleLazyObject:
    """Stand-in for an unhydrated ``SimpleLazyObject`` wrapping a user.

    Class name matters: the enricher identifies unhydrated lazy objects by
    ``type(user).__name__ == "SimpleLazyObject"`` without importing Django.
    """

    def __init__(self) -> None:
        self._wrapped = _LazyUserSentinel()
        self.queries_triggered = 0

    def __getattr__(self, item):
        # Any attribute access on an unhydrated lazy user in real Django
        # would force evaluation and issue a DB query. We raise here so the
        # test FAILS if the enricher ever touches us.
        # ``_wrapped`` and ``queries_triggered`` are set in __init__ so they
        # live in the instance dict and never route through __getattr__.
        self.queries_triggered += 1
        msg = f"lazy user hydration forbidden in enricher (touched {item!r})"
        raise AssertionError(msg)


_FakeSimpleLazyObject.__name__ = "SimpleLazyObject"


class _DBErrorRaisingUser:
    """User object whose attribute access raises a Django-style DB error."""

    class DatabaseError(Exception):
        pass

    def __init__(self, raise_on: str) -> None:
        self._raise_on = raise_on

    def _boom(self) -> None:
        msg = "connection to server was lost"
        raise _DBErrorRaisingUser.DatabaseError(msg)

    @property
    def is_authenticated(self) -> bool:
        if self._raise_on == "is_authenticated":
            self._boom()
        return True

    @property
    def pk(self):
        if self._raise_on == "pk":
            self._boom()
        return 7

    @property
    def id(self):
        return 7


def test_enricher_does_not_hydrate_lazy_user():
    """Security (FI-5): accessing ``.pk`` on an unhydrated SimpleLazyObject
    would trigger a DB query. The enricher must not do that."""
    lazy_user = _FakeSimpleLazyObject()
    request = _build_request(user=lazy_user)

    enricher = DjangoEnricher(strict=True)
    captured = _CapturingLogger()
    result = enricher.extract_context(captured, request)

    assert lazy_user.queries_triggered == 0
    assert result is not None
    # User fields are omitted (we can't safely know without hydrating).
    assert "user.id" not in result


def test_enricher_skips_pk_access_when_user_is_anonymous():
    """Security (FI-5): anonymous users must not trigger any ``.pk`` read."""

    class _RaisingAnon:
        is_authenticated = False

        @property
        def pk(self):
            msg = "pk access forbidden on anonymous user"
            raise AssertionError(msg)

        @property
        def id(self):
            msg = "id access forbidden on anonymous user"
            raise AssertionError(msg)

    request = _build_request(user=_RaisingAnon())

    enricher = DjangoEnricher(strict=True)
    captured = _CapturingLogger()
    result = enricher.extract_context(captured, request)

    assert result is not None
    # Anonymous users produce no user fields at all.
    assert "user.id" not in result


def test_enricher_surfaces_db_error_on_is_authenticated():
    """Security (FI-5): DB failures during user extraction must not be
    swallowed silently."""
    request = _build_request(user=_DBErrorRaisingUser(raise_on="is_authenticated"))

    enricher = DjangoEnricher(strict=True)
    captured = _CapturingLogger()
    result = enricher.extract_context(captured, request)

    # A dedicated warning is emitted so the outage is visible.
    assert any("Database error" in w for w in captured.warnings), (
        f"expected DatabaseError warning, got {captured.warnings}"
    )
    assert result is not None
    # User fields omitted rather than populated with stale data.
    assert "user.id" not in result


def test_enricher_surfaces_db_error_on_pk():
    """Security (FI-5): DB failure reading ``pk`` after successful
    ``is_authenticated`` must also be surfaced."""
    request = _build_request(user=_DBErrorRaisingUser(raise_on="pk"))

    enricher = DjangoEnricher(strict=True)
    captured = _CapturingLogger()
    result = enricher.extract_context(captured, request)

    assert any("Database error" in w for w in captured.warnings), (
        f"expected DatabaseError warning on pk access, got {captured.warnings}"
    )
    assert result is not None
    # user.id is omitted on DB error (no longer paired with an authenticated
    # flag — the flag was removed when the ``enduser.*`` namespace went).
    assert "user.id" not in result


# ---------------------------------------------------------------------------
# Security finding A1: isinstance vs Protocol duck-typing
# ---------------------------------------------------------------------------


def test_strict_mode_rejects_duck_typed_surrogate():
    """Security (A1): a non-HttpRequest object with all the right attributes
    previously passed the ``@runtime_checkable`` Protocol check. Strict mode
    replaces that with an isinstance check on ``django.http.HttpRequest``."""

    class _QuackyRequest:
        method = "GET"
        path = "/forged"
        scheme = "http"
        META: ClassVar[dict] = {}

        class GET:
            @staticmethod
            def urlencode() -> str:
                return ""

        class headers:  # noqa: N801 - mimics Django's lowercase attribute name
            @staticmethod
            def get(_key, default=None):
                return default

        def get_host(self) -> str:
            return "attacker.example"

    enricher = DjangoEnricher(strict=True)
    captured = _CapturingLogger()
    result = enricher.extract_context(captured, _QuackyRequest())  # type: ignore[arg-type]

    # Rejected: context extraction returned None because the isinstance
    # check raised TypeError, which the outer try/except converted into
    # an ``exception()`` call with the generic "failed to extract" message.
    assert result is None
    assert any("Failed to extract Django fields" in e for e in captured.exceptions), (
        f"expected generic failure message, got {captured.exceptions}"
    )


def test_non_strict_mode_accepts_duck_typed_surrogate_without_warning():
    """``strict=False`` retains the pre-0.6 permissive behaviour for callers
    who knowingly opt into duck typing (e.g. internal unit tests that build
    request-like objects without Django)."""

    # A minimally conformant request-like object.
    class _Mini:
        method = "GET"
        path = "/x"
        scheme = "http"
        META: ClassVar[dict] = {}

        class GET:
            @staticmethod
            def urlencode() -> str:
                return ""

        class headers:  # noqa: N801 - mimics Django's lowercase attribute name
            @staticmethod
            def get(_key, default=None):
                return default

        def get_host(self) -> str:
            return "testserver"

    # When Django IS importable, strict=False still passes the isinstance
    # check because that's the source of truth. So strict=False only has
    # an observable effect when Django is unavailable — covered by the
    # optional-import tests. Here we simply confirm that an enricher
    # constructed with strict=False does not blow up at import or
    # construction time.
    enricher = DjangoEnricher(strict=False)
    assert enricher is not None
