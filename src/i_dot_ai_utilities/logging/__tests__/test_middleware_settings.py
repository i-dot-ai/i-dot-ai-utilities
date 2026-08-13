# mypy: disable-error-code="no-untyped-def"
"""Tests for ``middleware._settings`` — logger resolution and the bare
``_BareStructlogAdapter`` fallback.

These tests exist to prevent regression of:

- Security finding A4: ``I_DOT_AI_LOGGER`` MUST refuse dotted-import strings.
  Accepting them would give anyone able to influence Django settings the
  ability to import arbitrary modules and invoke their zero-arg callables
  during middleware boot — code execution at init.
- Constitution Art. 22: the default logger path MUST NOT call
  ``structlog.configure()`` (directly or transitively). Doing so globally
  reconfigures structlog inside an *observability library*, which is
  widely considered an anti-pattern.
- The five-method logger contract the middleware writes through
  (``info``/``warning``/``error``/``exception``/``refresh_context``/
  ``set_context_field``). The ``_BareStructlogAdapter`` is the default
  when no ``I_DOT_AI_LOGGER`` is configured; if its behaviour regresses,
  consumers get silent no-ops.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
import structlog

django = pytest.importorskip("django")

from django.conf import settings as django_settings  # type: ignore[import-untyped]  # noqa: E402

if not django_settings.configured:
    django_settings.configure(
        DEBUG=False,
        ALLOWED_HOSTS=["*"],
        DATABASES={},
        INSTALLED_APPS=[],
        MIDDLEWARE=[],
        ROOT_URLCONF=__name__,
        SECRET_KEY="test-secret-not-for-production",
        USE_TZ=True,
    )
    django.setup()

from django.core.exceptions import ImproperlyConfigured  # type: ignore[import-untyped]  # noqa: E402

from i_dot_ai_utilities.logging.middleware._settings import (  # noqa: E402
    _BareStructlogAdapter,
    _default_logger,
    resolve_logger,
)
from i_dot_ai_utilities.logging.types.enrichment_types import (  # noqa: E402
    ContextEnrichmentType,
)

urlpatterns: list = []


# ---------------------------------------------------------------------------
# Logger doubles
# ---------------------------------------------------------------------------


class _ConformantLogger:
    """Satisfies the five-method ``_StructuredLoggerLike`` Protocol."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def info(self, message_template: str, **kwargs: Any) -> None:
        self.calls.append(("info", message_template, kwargs))

    def warning(self, message_template: str, **kwargs: Any) -> None:
        self.calls.append(("warning", message_template, kwargs))

    def error(self, message_template: str, **kwargs: Any) -> None:
        self.calls.append(("error", message_template, kwargs))

    def exception(self, message_template: str, **kwargs: Any) -> None:
        self.calls.append(("exception", message_template, kwargs))

    def refresh_context(
        self,
        context_enrichers: Any | None = None,
        scope: str = "manual",
    ) -> None:
        self.calls.append(("refresh_context", scope, {"enrichers": context_enrichers}))

    def set_context_field(self, field_key: str, field_value: Any) -> None:
        self.calls.append(("set_context_field", field_key, {"value": field_value}))


class _NonConformantLogger:
    """Missing one of the required methods — must be rejected."""

    def info(self, *_a: Any, **_k: Any) -> None:
        pass

    # no warning / error / exception / refresh_context / set_context_field


# ---------------------------------------------------------------------------
# resolve_logger — security finding A4 + general dispatch
# ---------------------------------------------------------------------------


class TestResolveLogger:
    def test_dotted_string_is_rejected_with_a4_message(self):
        """Finding A4: dotted-import strings must never be resolved — that
        was the previous attack surface allowing arbitrary imports at boot."""
        with pytest.raises(ImproperlyConfigured) as excinfo:
            resolve_logger("mypkg.mymodule.build_logger")

        message = str(excinfo.value)
        # The error message must name the setting AND the remediation path
        # so operators can act on it without reading source.
        assert "I_DOT_AI_LOGGER" in message
        assert "dotted-import string" in message
        assert "settings.py" in message

    def test_logger_object_passes_through_unchanged(self):
        logger = _ConformantLogger()
        assert resolve_logger(logger) is logger

    def test_zero_arg_callable_is_invoked_and_result_returned(self):
        logger: _ConformantLogger = _ConformantLogger()

        def factory() -> _ConformantLogger:
            return logger

        resolved = resolve_logger(factory)
        assert resolved is logger

    def test_callable_returning_non_conformant_object_raises(self):
        def factory() -> _NonConformantLogger:
            return _NonConformantLogger()

        with pytest.raises(ImproperlyConfigured) as excinfo:
            resolve_logger(factory)
        # Error message names the protocol contract so operators
        # understand why their logger was refused.
        msg = str(excinfo.value)
        assert "not a structured logger" in msg
        assert "info/warning/error/exception/refresh_context" in msg

    def test_callable_that_raises_is_wrapped_as_improperly_configured(self):
        class FactoryError(RuntimeError):
            pass

        def broken_factory() -> Any:
            msg = "cannot build logger"
            raise FactoryError(msg)

        with pytest.raises(ImproperlyConfigured) as excinfo:
            resolve_logger(broken_factory)
        # The original exception should be chained (Python __cause__)
        # so tracebacks remain useful, and the outer message should
        # name the underlying exception class.
        assert isinstance(excinfo.value.__cause__, FactoryError)
        assert "FactoryError" in str(excinfo.value)

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(42, id="int"),
            pytest.param([1, 2, 3], id="list"),
            pytest.param({"x": 1}, id="dict"),
        ],
    )
    def test_unsupported_type_is_rejected(self, value):
        with pytest.raises(ImproperlyConfigured) as excinfo:
            resolve_logger(value)
        assert "must be a callable or a logger object" in str(excinfo.value)

    def test_none_returns_default_bare_adapter(self):
        resolved = resolve_logger(None)
        assert isinstance(resolved, _BareStructlogAdapter)


# ---------------------------------------------------------------------------
# _BareStructlogAdapter — default "no configuration" path
# ---------------------------------------------------------------------------


class TestBareStructlogAdapter:
    def test_info_warning_error_exception_delegate_through(self):
        inner = _ConformantLogger()
        adapter = _BareStructlogAdapter(inner)

        adapter.info("i", a=1)
        adapter.warning("w", b=2)
        adapter.error("e", c=3)
        adapter.exception("x", d=4)

        assert [c[0] for c in inner.calls] == ["info", "warning", "error", "exception"]
        assert inner.calls[0] == ("info", "i", {"a": 1})
        assert inner.calls[3] == ("exception", "x", {"d": 4})

    def test_set_context_field_binds_to_structlog_contextvars(self):
        # Clear any prior state so the assertion is deterministic.
        structlog.contextvars.clear_contextvars()
        try:
            adapter = _BareStructlogAdapter(_ConformantLogger())
            adapter.set_context_field("tenant_id", "abc-123")

            bound = structlog.contextvars.get_contextvars()
            assert bound.get("tenant_id") == "abc-123"
        finally:
            structlog.contextvars.clear_contextvars()

    def test_refresh_context_with_no_enrichers_clears_contextvars(self):
        try:
            structlog.contextvars.bind_contextvars(leftover="value")
            assert structlog.contextvars.get_contextvars().get("leftover") == "value"

            adapter = _BareStructlogAdapter(_ConformantLogger())
            adapter.refresh_context()

            assert "leftover" not in structlog.contextvars.get_contextvars()
        finally:
            structlog.contextvars.clear_contextvars()

    def test_refresh_context_applies_fastapi_enricher_fields(self):
        from starlette.datastructures import Headers  # noqa: PLC0415
        from starlette.requests import Request  # noqa: PLC0415

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/x",
            "raw_path": b"/x",
            "query_string": b"q=1",
            "headers": Headers({"host": "testserver"}).raw,
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "http_version": "1.1",
            "root_path": "",
        }
        request = Request(scope)

        try:
            adapter = _BareStructlogAdapter(_ConformantLogger())
            adapter.refresh_context(
                context_enrichers=[
                    {"type": ContextEnrichmentType.FASTAPI, "object": request},
                ],
            )

            bound = structlog.contextvars.get_contextvars()
            # FastApiEnricher nests HTTP fields under ``request``.
            assert bound.get("request", {}).get("method") == "GET"
        finally:
            structlog.contextvars.clear_contextvars()

    def test_refresh_context_silently_skips_malformed_enrichers(self):
        """Missing ``type`` or ``object`` keys must be tolerated — copy-paste
        bugs in consumer code must not take down the request.
        """
        try:
            adapter = _BareStructlogAdapter(_ConformantLogger())
            adapter.refresh_context(
                context_enrichers=[
                    {"type": None, "object": None},  # both missing -> skip
                    {"object": "no type key"},  # type missing
                    {"type": ContextEnrichmentType.FASTAPI},  # object missing
                ],
            )
            # No exception raised, no fields bound.
            assert structlog.contextvars.get_contextvars() == {}
        finally:
            structlog.contextvars.clear_contextvars()

    def test_refresh_context_accepts_scope_kwarg_for_parity(self):
        """The adapter accepts ``scope=...`` but has no ownership registry;
        this test documents the interface contract."""
        try:
            adapter = _BareStructlogAdapter(_ConformantLogger())
            # Must not raise for any of the known scope values.
            adapter.refresh_context(scope="request")
            adapter.refresh_context(scope="job")
            adapter.refresh_context(scope="manual")
        finally:
            structlog.contextvars.clear_contextvars()


# ---------------------------------------------------------------------------
# Default path does not mutate structlog globally (Art. 22)
# ---------------------------------------------------------------------------


class TestDefaultLoggerDoesNotConfigureStructlog:
    """Constitution Art. 22: the default ``I_DOT_AI_LOGGER`` fallback path
    MUST NOT call ``structlog.configure()``. Doing so from inside an
    observability library silently overrides the consumer's own structlog
    setup — an anti-pattern that has bitten us before.
    """

    def test_default_logger_does_not_call_structlog_configure(self):
        with patch.object(structlog, "configure") as spy:
            logger = _default_logger()
            assert isinstance(logger, _BareStructlogAdapter)
            assert spy.call_count == 0

    def test_resolve_logger_none_does_not_call_structlog_configure(self):
        """Same invariant via the public entry point."""
        with patch.object(structlog, "configure") as spy:
            resolved = resolve_logger(None)
            assert isinstance(resolved, _BareStructlogAdapter)
            assert spy.call_count == 0

    def test_default_logger_exposes_five_method_contract(self):
        """Smoke test: the default adapter must be usable end-to-end by the
        middleware without raising AttributeError."""
        logger = _default_logger()

        # Each of these is called by ``StructuredLoggingMiddlewareOTel`` at
        # some point in a request lifecycle; if any is missing the
        # middleware would crash at runtime.
        logger.info("hello")
        logger.warning("hello")
        logger.error("hello")
        logger.exception("hello")
        logger.set_context_field("k", "v")
        logger.refresh_context()
