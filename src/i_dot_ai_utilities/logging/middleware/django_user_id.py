"""Django ``DjangoUserIdMiddleware`` — bind ``user.id`` to structlog context.

The OTel-backed ``StructuredLoggingMiddlewareOTel`` deliberately drops
``user.id`` extraction: OpenTelemetry's Django auto-instrumentation does
not surface the authenticated user, and baking the extraction into the
main middleware would re-entangle this library with Django's auth model.

This thin middleware fills that gap for consumers who want authenticated
user attribution on log records. It lifts the safety work that used to
live inside the deleted ``DjangoEnricher``:

- Finding FI-5: observability code MUST NOT issue a database query.
  ``request.user`` is often a ``django.utils.functional.SimpleLazyObject``
  that hydrates via the auth backend on first attribute access. Touching
  ``.pk`` on an unhydrated instance triggers a ``User.objects.get(...)``,
  which both adds query load and masks DB outages (the query fails, the
  log line silently loses ``user.id``, the operator can't correlate).
  We detect the unhydrated state structurally — class-name + ``_wrapped``
  sentinel — without importing Django.

- Art. 46 / resilience: any database-layer exception raised during
  attribute access is surfaced via a WARNING rather than swallowed so
  DB outages remain visible in the very logs that would diagnose them.
  Detection is by MRO class-name match (``DatabaseError`` and
  subclasses) so this module stays importable without ``django.db``.

Wire into ``settings.MIDDLEWARE`` *after* Django's
``AuthenticationMiddleware`` (so ``request.user`` exists) AND *after*
``StructuredLoggingMiddlewareOTel`` (so the request scope is active and
``set_context_field`` lands on the correct request context)::

    MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "i_dot_ai_utilities.logging.middleware.django_otel.StructuredLoggingMiddlewareOTel",
        "i_dot_ai_utilities.logging.middleware.django_user_id.DjangoUserIdMiddleware",
        # ... your other middleware
    ]

Sync-only by design (``sync_capable = True``, ``async_capable = False``)
to match ``StructuredLoggingMiddlewareOTel``.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any, ClassVar, Final

from django.conf import settings  # type: ignore[import-untyped]
from django.core.exceptions import MiddlewareNotUsed  # type: ignore[import-untyped]

from i_dot_ai_utilities.logging.middleware._settings import (
    SETTING_ENABLED,
    SETTING_LOGGER,
    resolve_logger,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from django.http import HttpRequest, HttpResponse  # type: ignore[import-untyped]


# Django database-layer exception class names. Matched structurally by MRO
# class name so this module can stay free of a top-level ``django.db``
# import (verified by ``test_optional_django_import.py``'s defence-in-depth
# grep). The set mirrors the Django DB exception hierarchy that has been
# stable since 1.x; if a new subclass ever slips through, the outer
# ``except Exception`` still catches it — we just lose the dedicated
# WARNING and fall back to silent skip.
_DB_ERROR_CLASS_NAMES: Final[frozenset[str]] = frozenset(
    {
        "DatabaseError",
        "OperationalError",
        "InterfaceError",
        "ProgrammingError",
        "IntegrityError",
    }
)


def _is_database_error(exc: BaseException) -> bool:
    """Return True if ``exc`` looks like a Django database-layer exception."""
    return any(cls.__name__ in _DB_ERROR_CLASS_NAMES for cls in type(exc).__mro__)


def _looks_like_unhydrated_lazy_object(user: Any) -> bool:
    """Detect Django's ``SimpleLazyObject`` before it has hydrated.

    Detection is by class name + the documented ``_wrapped`` sentinel so
    this module doesn't need to import Django's ``functional`` module.
    When the sentinel attribute is missing, or the wrapped value is the
    ``empty`` sentinel (class name ``"empty"``), the lazy object has not
    yet been evaluated and ``.pk`` access would trigger a DB query. We
    return True to skip.
    """
    if type(user).__name__ != "SimpleLazyObject":
        return False
    wrapped = getattr(user, "_wrapped", None)
    if wrapped is None:
        return True
    return type(wrapped).__name__ == "empty"


class DjangoUserIdMiddleware:
    """Bind ``user.id`` onto the structlog context after authentication runs.

    Emits nothing when ``request.user`` is absent, anonymous, or an
    unhydrated lazy object. Emits a WARNING when a database-layer
    exception is raised while reading the user — the request continues
    unaffected.
    """

    sync_capable: ClassVar[bool] = True
    async_capable: ClassVar[bool] = False

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self._get_response = get_response

        enabled = getattr(settings, SETTING_ENABLED, True)
        if not enabled:
            # Mirror the other middlewares: clean removal via MiddlewareNotUsed
            # rather than a silent no-op.
            msg = "DjangoUserIdMiddleware disabled via settings."
            raise MiddlewareNotUsed(msg)

        self._logger = resolve_logger(getattr(settings, SETTING_LOGGER, None))

    def __call__(self, request: HttpRequest) -> HttpResponse:
        # Bind first, then call through. Binding after the view would miss
        # log events emitted inside the view itself — the whole point is to
        # tag every in-request log line with the acting user.
        self._bind_user_id(request)
        return self._get_response(request)

    def process_exception(
        self,
        request: HttpRequest,
        exception: BaseException,
    ) -> None:
        """No-op: matches the finding-A7 contract on the other middlewares.

        ``request`` and ``exception`` are accepted for parity with Django's
        middleware protocol; intentionally unused.
        """
        # Explicitly no return: None signals "keep looking".

    # --- private helpers ----------------------------------------------------

    def _bind_user_id(self, request: HttpRequest) -> None:
        """Best-effort ``user.id`` extraction. Never raises."""
        try:
            user_id = self._extract_user_id(request)
        except Exception:  # noqa: BLE001 - observability must not break the request
            # Defence in depth: any path that escapes the inner handlers
            # still must not take the request down.
            return

        if user_id is not None:
            self._logger.set_context_field("user.id", user_id)

    def _extract_user_id(self, request: HttpRequest) -> str | None:
        """Return ``str(pk)`` for the authenticated user, or ``None``.

        Returns ``None`` for every "we shouldn't emit user.id" path:
        missing ``request.user``, unhydrated lazy object, anonymous or
        non-strictly-True ``is_authenticated``, attribute-access DB
        errors (which are surfaced via WARNING before returning), and
        ``pk``/``id`` both absent.
        """
        user = getattr(request, "user", None)
        if user is None:
            return None

        # FI-5: never force hydration of a lazy user.
        if _looks_like_unhydrated_lazy_object(user):
            return None

        if not self._user_is_authenticated(user):
            return None

        return self._read_user_pk(user)

    def _user_is_authenticated(self, user: Any) -> bool:
        """Safely read ``is_authenticated``. Returns False on DB errors
        (after emitting a WARNING) or any other failure.
        """
        try:
            is_authenticated_attr = getattr(user, "is_authenticated", None)
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment
            if _is_database_error(exc):
                self._warn_db_error(
                    "Warning(Logger): Database error reading request.user.is_authenticated; "
                    "user.id omitted from log context"
                )
            return False
        # Strict ``is True`` per the FI-5 contract: truthy-but-not-True
        # sentinels (mock objects, duck-typed surrogates) must not cause
        # a pk lookup.
        return is_authenticated_attr is True

    def _read_user_pk(self, user: Any) -> str | None:
        """Read ``pk`` (or ``id`` as fallback) and stringify it. Returns
        ``None`` on DB errors or when both are absent.
        """
        try:
            user_id_attr = getattr(user, "pk", None)
            if user_id_attr is None:
                user_id_attr = getattr(user, "id", None)
        except Exception as exc:  # noqa: BLE001 - best-effort enrichment
            if _is_database_error(exc):
                self._warn_db_error(
                    "Warning(Logger): Database error reading request.user primary key; "
                    "user.id omitted from log context"
                )
            return None

        if user_id_attr is None:
            return None
        return str(user_id_attr)

    def _warn_db_error(self, message: str) -> None:
        """Emit a WARNING without letting a broken logger crash the request."""
        try:
            self._logger.warning(message)
        except Exception as exc:  # noqa: BLE001 - last-resort observability
            print(  # noqa: T201
                f"i_dot_ai_utilities: user.id db-error warning failed: {type(exc).__name__}: {exc}",
                file=sys.stderr,
            )


__all__ = ["DjangoUserIdMiddleware"]
