"""Settings names and logger-resolution helper for the Django middleware.

Setting names are namespaced (``I_DOT_AI_LOGGING_...``) so they cannot collide
with Django's own ``LOGGING`` setting. Django-specific imports are deliberately
**lazy** (inside ``resolve_logger``) so this module stays importable in
non-Django consumers — verified by ``test_optional_django_import.py``.

Security note (finding A4): ``I_DOT_AI_LOGGER`` deliberately does NOT accept
dotted-import strings. A string-based configuration value would let anyone
who can influence Django settings (env var, settings override, Helm values)
trigger ``import_string`` and ``__call__`` arbitrary modules during
middleware boot — executing attacker code before any view or auth check.
Consumers must pass a fully-constructed logger object or a zero-arg
callable imported in their own ``settings.py``.
"""

from __future__ import annotations

from typing import Any, Final, Protocol, runtime_checkable

# --- Setting-name constants -------------------------------------------------

SETTING_LOGGER: Final[str] = "I_DOT_AI_LOGGER"
SETTING_ENABLED: Final[str] = "I_DOT_AI_LOGGING_MIDDLEWARE_ENABLED"
SETTING_EXCLUDED_PREFIXES: Final[str] = "I_DOT_AI_LOGGING_EXCLUDED_PREFIXES"
SETTING_EXCLUDED_REGEXES: Final[str] = "I_DOT_AI_LOGGING_EXCLUDED_REGEXES"
SETTING_HEADER_ALLOWLIST: Final[str] = "I_DOT_AI_LOGGING_HEADER_ALLOWLIST"


# --- Logger resolution ------------------------------------------------------


@runtime_checkable
class _StructuredLoggerLike(Protocol):
    """Minimal duck-typed shape for an acceptable logger object.

    The library's own ``StructuredLogger`` satisfies this, but so does anything
    else that exposes the five method names used by the middleware. Kept as a
    ``runtime_checkable`` Protocol so we can validate user-supplied objects
    cleanly.

    ``refresh_context`` accepts an optional ``scope`` keyword introduced
    alongside the request-scope ownership check in ``StructuredLogger``.
    Custom loggers may ignore it — the middleware always passes it by keyword
    so positional-only implementations continue to work.
    """

    def info(self, message_template: str, **kwargs: Any) -> None: ...
    def warning(self, message_template: str, **kwargs: Any) -> None: ...
    def error(self, message_template: str, **kwargs: Any) -> None: ...
    def exception(self, message_template: str, **kwargs: Any) -> None: ...
    def refresh_context(
        self,
        context_enrichers: Any | None = None,
        scope: str = "manual",
    ) -> None: ...
    def set_context_field(self, field_key: str, field_value: Any) -> None: ...


class _BareStructlogAdapter:
    """Adapter that surfaces the five-method contract over a plain structlog
    bound logger.

    The middleware's default path (no ``I_DOT_AI_LOGGER`` configured) returns
    ``structlog.get_logger(__name__)``. That object exposes
    ``info``/``warning``/``error``/``exception`` but not ``refresh_context``
    / ``set_context_field`` — the latter are library-specific conveniences.
    Wrapping keeps the middleware's call sites uniform and honours Art. 22
    by never calling ``structlog.configure``.

    ``refresh_context`` clears ``structlog.contextvars`` and invokes any
    supplied enrichers directly through ``EnrichmentProvider``. That matches
    what ``StructuredLogger.refresh_context`` does for non-owned scopes. The
    ``scope`` argument is accepted for interface parity; there is no
    per-request ownership registry in the bare-logger path (consumers wanting
    ownership enforcement should pass ``StructuredLogger``).
    """

    __slots__ = ("_logger", "_provider")

    def __init__(self, logger: Any) -> None:
        self._logger = logger
        # The provider is lazily created on first ``refresh_context`` call so
        # importing this module cannot pull in enricher-side code paths that
        # should only run at request time.
        self._provider: Any = None

    def info(self, message_template: str, **kwargs: Any) -> None:
        self._logger.info(message_template, **kwargs)

    def warning(self, message_template: str, **kwargs: Any) -> None:
        self._logger.warning(message_template, **kwargs)

    def error(self, message_template: str, **kwargs: Any) -> None:
        self._logger.error(message_template, **kwargs)

    def exception(self, message_template: str, **kwargs: Any) -> None:
        self._logger.exception(message_template, **kwargs)

    def set_context_field(self, field_key: str, field_value: Any) -> None:
        import structlog  # noqa: PLC0415

        structlog.contextvars.bind_contextvars(**{field_key: field_value})

    def refresh_context(
        self,
        context_enrichers: Any | None = None,
        scope: str = "manual",  # noqa: ARG002 - parity with StructuredLogger
    ) -> None:
        import structlog  # noqa: PLC0415

        structlog.contextvars.clear_contextvars()
        if not context_enrichers:
            return

        # Lazy-build the provider so non-Django consumers don't pay for it.
        if self._provider is None:
            from i_dot_ai_utilities.logging.enrichers.enrichment_provider import (  # noqa: PLC0415
                EnrichmentProvider,
            )
            from i_dot_ai_utilities.logging.types.enrichment_types import (  # noqa: PLC0415
                ExecutionEnvironmentType,
            )

            self._provider = EnrichmentProvider(ExecutionEnvironmentType.LOCAL)

        combined: dict[str, Any] = {}
        for spec in context_enrichers:
            enricher_type = spec.get("type") if isinstance(spec, dict) else None
            target = spec.get("object") if isinstance(spec, dict) else None
            if enricher_type is None or target is None:
                continue
            extracted = self._provider.extract_context_from_framework_enricher(self._logger, enricher_type, target)
            if extracted:
                combined.update(extracted)
        if combined:
            structlog.contextvars.bind_contextvars(**combined)


def _default_logger() -> Any:
    """Fallback logger when no setting is provided.

    Returns a ``_BareStructlogAdapter`` over ``structlog.get_logger(__name__)``.
    This deliberately does NOT instantiate the library's ``StructuredLogger`` —
    the latter's ``__init__`` calls
    ``ProcessorHelper().configure_processors(...)``, which globally mutates
    ``structlog`` configuration. The middleware is forbidden from calling
    ``structlog.configure()`` (constitution Art. 22), so the default path must
    not trigger it transitively either. Consumers who want the full
    ``StructuredLogger`` experience should construct one in their own
    ``settings.py`` and pass it via ``I_DOT_AI_LOGGER``.
    """
    import structlog  # noqa: PLC0415

    return _BareStructlogAdapter(structlog.get_logger(__name__))


def _call_callable(
    raw: Any,
    label: str,
) -> Any:
    """Invoke a zero-arg callable, raising ``ImproperlyConfigured`` on failure.

    Factored out so ``resolve_logger`` stays simple and the exception-to-
    ImproperlyConfigured adaptation is applied consistently.
    """
    from django.core.exceptions import ImproperlyConfigured  # type: ignore[import-untyped]  # noqa: PLC0415

    try:
        produced = raw()
    except Exception as exc:
        msg = f"i_dot_ai_utilities logging: {label} raised {type(exc).__name__}: {exc}."
        raise ImproperlyConfigured(msg) from exc
    if isinstance(produced, _StructuredLoggerLike):
        return produced
    msg = (
        f"i_dot_ai_utilities logging: {label} returned "
        f"{type(produced).__name__!s}, which is not a structured logger "
        f"(must expose info/warning/error/exception/refresh_context)."
    )
    raise ImproperlyConfigured(msg)


def resolve_logger(raw: Any) -> Any:
    """Resolve a user-supplied ``I_DOT_AI_LOGGER`` setting into a logger object.

    Accepts any of:

    - ``None`` / absent: returns a default ``StructuredLogger`` instance.
    - Zero-arg callable: invoked, result validated.
    - Any object that quacks like a structured logger: returned as-is.

    **Does NOT accept dotted-import strings.** Security finding A4: a string
    configuration value would give anyone able to influence Django settings
    (env var, settings override, Helm values, shared config) the ability to
    import arbitrary Python modules and invoke their zero-arg callables
    during middleware boot — effectively code-execution-at-init. Consumers
    must import their logger in their own ``settings.py`` and assign the
    object/callable directly.

    Raises ``django.core.exceptions.ImproperlyConfigured`` on malformed input.
    Django imports are performed lazily inside this function so the module
    stays importable for non-Django consumers.
    """
    if raw is None:
        return _default_logger()

    from django.core.exceptions import ImproperlyConfigured  # type: ignore[import-untyped]  # noqa: PLC0415

    if isinstance(raw, str):
        # Security (finding A4): strings are forbidden. Produce a clear
        # message so operators understand why and how to migrate.
        msg = (
            f"i_dot_ai_utilities logging: {SETTING_LOGGER} no longer accepts "
            f"a dotted-import string. Pass a fully-constructed logger object "
            f"or a zero-arg callable imported in your settings.py. This "
            f"closes a boot-time arbitrary-import attack surface "
            f"(see release notes for 0.6.0)."
        )
        raise ImproperlyConfigured(msg)

    if isinstance(raw, _StructuredLoggerLike):
        return raw

    if callable(raw):
        return _call_callable(raw, label=f"{SETTING_LOGGER} callable")

    msg = (
        f"i_dot_ai_utilities logging: {SETTING_LOGGER} must be a callable or a logger object. Got {type(raw).__name__}."
    )
    raise ImproperlyConfigured(msg)
