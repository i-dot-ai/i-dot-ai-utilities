# mypy: disable-error-code="no-untyped-def"
"""Unit tests for ``DjangoUserIdMiddleware``.

The middleware is the replacement for the deleted ``DjangoEnricher``'s
user-extraction path. These tests specifically target the hardening
lifted from that enricher: FI-5 (no DB query on unhydrated lazy users),
database-error-surfacing via WARNING, and the anonymous/authenticated
branches.

``RecordingLogger`` captures ``set_context_field`` / ``warning`` calls
so assertions can be made without configuring structlog.
"""

from __future__ import annotations

from typing import Any

import pytest

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

from django.core.exceptions import MiddlewareNotUsed  # type: ignore[import-untyped]  # noqa: E402
from django.http import HttpResponse  # type: ignore[import-untyped]  # noqa: E402
from django.test import RequestFactory  # type: ignore[import-untyped]  # noqa: E402

from i_dot_ai_utilities.logging.middleware.django_user_id import (  # noqa: E402
    DjangoUserIdMiddleware,
)

urlpatterns: list = []


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class RecordingLogger:
    """Structured-logger double matching the middleware's protocol."""

    def __init__(self) -> None:
        self.context: dict[str, Any] = {}
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.exceptions: list[str] = []
        self.refresh_calls: list[tuple[list[Any] | None, str]] = []

    def info(self, message_template: str, **_kwargs: Any) -> None:
        self.infos.append(message_template)

    def warning(self, message_template: str, **_kwargs: Any) -> None:
        self.warnings.append(message_template)

    def error(self, message_template: str, **_kwargs: Any) -> None:
        self.errors.append(message_template)

    def exception(self, message_template: str, **_kwargs: Any) -> None:
        self.exceptions.append(message_template)

    def set_context_field(self, key: str, value: Any) -> None:
        self.context[key] = value

    def refresh_context(
        self,
        context_enrichers: list[Any] | None = None,
        scope: str = "manual",
    ) -> None:
        self.refresh_calls.append((context_enrichers, scope))


class _FakeUser:
    """Minimal duck-typed user object."""

    def __init__(
        self,
        *,
        is_authenticated: bool = True,
        pk: Any = 42,
        id: Any = None,  # noqa: A002 - mirrors Django's user attr name
    ) -> None:
        self.is_authenticated = is_authenticated
        self.pk = pk
        self.id = id


class _FakeSimpleLazyObject:
    """Fake Django ``SimpleLazyObject`` — class name matches what the
    middleware detects structurally without importing Django.
    """

    __name__ = "SimpleLazyObject"

    def __init__(self, wrapped: Any = None) -> None:
        self._wrapped = wrapped

    def __class__(self) -> type:  # type: ignore[override]
        return type(self)


# We need the class name literally "SimpleLazyObject" so the detection
# heuristic fires. Create it dynamically.
SimpleLazyObject = type(
    "SimpleLazyObject",
    (),
    {
        "__init__": _FakeSimpleLazyObject.__init__,
    },
)


class _EmptySentinel:
    """Fake of Django's ``django.utils.functional.empty`` sentinel.

    Detected by class name ``"empty"``.
    """


# Name it literally ``empty`` so the structural detection hits.
empty = type("empty", (), {})


def _make_lazy(wrapped: Any = None) -> Any:
    obj = SimpleLazyObject()
    obj._wrapped = wrapped  # noqa: SLF001 - setting fixture internals
    return obj


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
# Happy paths
# ---------------------------------------------------------------------------


class TestAuthenticatedUser:
    def test_binds_user_id_from_pk(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        request.user = _FakeUser(is_authenticated=True, pk=99)

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        mw(request)

        assert logger.context == {"user.id": "99"}

    def test_falls_back_to_id_when_pk_is_none(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        request.user = _FakeUser(is_authenticated=True, pk=None, id="user-xyz")

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        mw(request)

        assert logger.context == {"user.id": "user-xyz"}

    def test_stringifies_non_string_pk(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        request.user = _FakeUser(is_authenticated=True, pk=1234)

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        mw(request)

        assert logger.context["user.id"] == "1234"
        assert isinstance(logger.context["user.id"], str)


# ---------------------------------------------------------------------------
# Branches that must NOT bind anything
# ---------------------------------------------------------------------------


class TestSkippedPaths:
    def test_anonymous_user_emits_nothing(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        request.user = _FakeUser(is_authenticated=False, pk=1)

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        mw(request)

        assert logger.context == {}
        assert logger.warnings == []

    def test_is_authenticated_not_exactly_true_is_treated_as_anonymous(
        self, rf, logger, settings_sandbox
    ):
        """Truthy-but-not-True values must not bind user.id.

        Defensive against duck-typed or mocked user objects whose
        ``is_authenticated`` is a truthy sentinel rather than a real bool.
        """
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        request.user = _FakeUser(is_authenticated="yes", pk=1)  # type: ignore[arg-type]

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        mw(request)

        assert logger.context == {}

    def test_missing_user_attr_emits_nothing(self, rf, logger, settings_sandbox):
        """Auth middleware hasn't run — absence of request.user is fine."""
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        # Deliberately do NOT set request.user.

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        mw(request)

        assert logger.context == {}
        assert logger.warnings == []

    def test_authenticated_but_pk_and_id_both_none_emits_nothing(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        request.user = _FakeUser(is_authenticated=True, pk=None, id=None)

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        mw(request)

        assert logger.context == {}


class TestUnhydratedLazyObject:
    """FI-5: accessing .pk on an unhydrated SimpleLazyObject forces a DB
    query via the auth backend. We must skip without touching it."""

    def test_lazy_with_empty_wrapped_is_skipped(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        # Wrap an instance of an ``empty``-named class — mirrors Django's
        # module-level ``empty`` sentinel on an unhydrated SimpleLazyObject.
        request.user = _make_lazy(wrapped=empty())

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        mw(request)

        assert logger.context == {}
        assert logger.warnings == []

    def test_lazy_with_none_wrapped_is_skipped(self, rf, logger, settings_sandbox):
        """Some older Django versions use None as the unhydrated sentinel."""
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        request.user = _make_lazy(wrapped=None)

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        mw(request)

        assert logger.context == {}

    def test_lazy_hydrated_falls_through_to_normal_path(
        self, rf, logger, settings_sandbox
    ):
        """A lazy wrapper whose ``_wrapped`` is a real user must NOT be
        treated as unhydrated — we drop into the normal extraction path.

        Note: the hydrated user attribute lookups happen on the lazy
        wrapper itself (our fake doesn't proxy like the real
        SimpleLazyObject does), so we set them directly on the wrapper
        to simulate the effective behaviour after hydration.
        """
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        real_user = _FakeUser(is_authenticated=True, pk=7)
        lazy = _make_lazy(wrapped=real_user)
        # Fake proxy behaviour: copy user attrs onto the wrapper.
        lazy.is_authenticated = real_user.is_authenticated
        lazy.pk = real_user.pk
        lazy.id = real_user.id
        request.user = lazy

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        mw(request)

        assert logger.context == {"user.id": "7"}


# ---------------------------------------------------------------------------
# Database error surfacing
# ---------------------------------------------------------------------------


class _ExplodingUserIsAuth:
    """User object whose ``is_authenticated`` access raises OperationalError."""

    class _OperationalError(Exception):
        """Named to match Django's class name for MRO detection."""

    # Intentionally rename at class-creation time so the MRO name match
    # triggers in the middleware's ``_is_database_error`` helper.
    _OperationalError.__name__ = "OperationalError"

    @property
    def is_authenticated(self) -> bool:
        msg = "connection refused"
        raise self._OperationalError(msg)


class _ExplodingUserPk:
    """Authenticated user whose ``pk`` access raises DatabaseError."""

    class _DatabaseError(Exception):
        pass

    _DatabaseError.__name__ = "DatabaseError"

    is_authenticated = True

    @property
    def pk(self) -> Any:
        msg = "db gone"
        raise self._DatabaseError(msg)

    @property
    def id(self) -> Any:
        msg = "db gone"
        raise self._DatabaseError(msg)


class _NonDbExceptionOnIsAuth:
    """User whose ``is_authenticated`` raises a non-DB exception."""

    @property
    def is_authenticated(self) -> bool:
        msg = "something unrelated"
        raise ValueError(msg)


class TestDatabaseErrors:
    def test_db_error_on_is_authenticated_warns_and_continues(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        request.user = _ExplodingUserIsAuth()

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        response = mw(request)

        assert response.status_code == 200
        assert logger.context == {}
        assert len(logger.warnings) == 1
        assert "is_authenticated" in logger.warnings[0]
        assert "Database error" in logger.warnings[0]

    def test_db_error_on_pk_warns_and_continues(self, rf, logger, settings_sandbox):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        request.user = _ExplodingUserPk()

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        response = mw(request)

        assert response.status_code == 200
        assert logger.context == {}
        assert len(logger.warnings) == 1
        assert "primary key" in logger.warnings[0]
        assert "Database error" in logger.warnings[0]

    def test_non_db_exception_on_is_authenticated_is_silenced_without_warning(
        self, rf, logger, settings_sandbox
    ):
        """Non-DB errors are observability problems, not user-visible ones.

        The middleware must still not take the request down, but it also
        must not spam WARNINGs for every unrelated exception.
        """
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        request = rf.get("/")
        request.user = _NonDbExceptionOnIsAuth()

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        response = mw(request)

        assert response.status_code == 200
        assert logger.context == {}
        assert logger.warnings == []


# ---------------------------------------------------------------------------
# Middleware protocol / lifecycle
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_disabled_raises_middleware_not_used(self, settings_sandbox, logger):
        settings_sandbox(I_DOT_AI_LOGGING_MIDDLEWARE_ENABLED=False, I_DOT_AI_LOGGER=logger)

        with pytest.raises(MiddlewareNotUsed):
            DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))

    def test_process_exception_returns_none_and_leaves_context_untouched(
        self, rf, logger, settings_sandbox
    ):
        settings_sandbox(I_DOT_AI_LOGGER=logger)
        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))

        request = rf.get("/")
        result = mw.process_exception(request, RuntimeError("boom"))

        assert result is None
        # Processing the exception must not alter log context.
        assert logger.context == {}

    def test_sync_capable_flags(self):
        assert DjangoUserIdMiddleware.sync_capable is True
        assert DjangoUserIdMiddleware.async_capable is False

    def test_broken_logger_warning_falls_back_to_stderr_without_crashing(
        self, rf, settings_sandbox, capsys
    ):
        class BrokenLogger:
            def set_context_field(self, *_a: Any, **_k: Any) -> None:
                pass

            def warning(self, *_a: Any, **_k: Any) -> None:
                msg = "logger broken"
                raise RuntimeError(msg)

            # Minimum surface the resolve_logger protocol demands.
            def info(self, *_a: Any, **_k: Any) -> None:
                pass

            def error(self, *_a: Any, **_k: Any) -> None:
                pass

            def exception(self, *_a: Any, **_k: Any) -> None:
                pass

            def refresh_context(self, *_a: Any, **_k: Any) -> None:
                pass

        settings_sandbox(I_DOT_AI_LOGGER=BrokenLogger())
        request = rf.get("/")
        request.user = _ExplodingUserPk()

        mw = DjangoUserIdMiddleware(lambda _r: HttpResponse(status=200))
        # Must not raise even though both the user access AND the warning
        # path explode.
        response = mw(request)
        assert response.status_code == 200

        captured = capsys.readouterr()
        assert "user.id db-error warning failed" in captured.err
