"""Validate log attribute names against OpenTelemetry semantic conventions and the `i_ai` namespace."""

from __future__ import annotations

import functools
import importlib.util
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from opentelemetry.sdk._logs import LogRecordProcessor

from i_dot_ai_utilities.otel._environment import env_flag

if TYPE_CHECKING:
    from collections.abc import Mapping

    from opentelemetry.sdk._logs import ReadWriteLogRecord

I_AI_NAMESPACE: Final[str] = "i_ai"
UNNAMESPACED_PREFIX: Final[str] = f"{I_AI_NAMESPACE}.unnamespaced."
ENV_STRICT_ATTRIBUTES: Final[str] = "I_DOT_AI_OTEL_STRICT_ATTRIBUTES"
# Third-party framework loggers such as django.request attach unnamespaced
# extra keys outside application control. Rename them under i_ai.unnamespaced.*
# even in strict mode so framework logs do not crash test suites.
_THIRD_PARTY_STRICT_EXEMPT_LOGGERS: Final[tuple[str, ...]] = (
    "aiohttp",
    "alembic",
    "boto3",
    "botocore",
    "celery",
    "django",
    "flask",
    "gunicorn",
    "httpx",
    "sqlalchemy",
    "urllib3",
    "uvicorn",
    "werkzeug",
)
# AWS Lambda's built-in logging handler injects aws_request_id onto every record.
_LAMBDA_REQUEST_ID_KEY: Final[str] = "aws_request_id"
_INVOCATION_ID_ATTRIBUTE: Final[str] = "faas.invocation_id"
# Standard semantic convention attributes injected automatically onto log records by the SDK and Lambda runtime.
_SDK_LOG_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        "code.column.number",
        "code.file.path",
        "code.function.name",
        "code.line.number",
        "exception.message",
        "exception.stacktrace",
        "exception.type",
        _INVOCATION_ID_ATTRIBUTE,
    }
)

_SEGMENT: Final[str] = r"[a-z][a-z0-9_]*"
# Require at least i_ai.<service>.<property>, while allowing deeper <object>.<property> segments.
_I_AI_NAME: Final[re.Pattern[str]] = re.compile(rf"{I_AI_NAMESPACE}(\.{_SEGMENT}){{2,}}")
_SEMCONV_CONSTANT: Final[re.Pattern[str]] = re.compile(r'^([A-Z][A-Z0-9_]*): Final = "([^"]+)"', re.MULTILINE)
_SEMCONV_PACKAGES: Final[tuple[str, ...]] = (
    "opentelemetry.semconv.attributes",
    "opentelemetry.semconv._incubating.attributes",
)

_warned_keys: set[str] = set()


@functools.cache
def _semconv_registry() -> tuple[frozenset[str], tuple[str, ...]]:
    """Scan installed semantic convention modules for exact attribute names and template prefixes.

    Reads `*_attributes.py` source files as text instead of importing ~100
    modules, and `warm_semconv_registry()` populates this cache during
    `configure_otel()` so the disk scan never runs inside a request. Unit tests
    verify that this scan matches importing every module so an upstream format
    change fails CI.

    Returns:
        A `(names, template_prefixes)` pair where `names` contains exact
        semantic convention keys and `template_prefixes` contains sorted
        prefixes that include a trailing dot.
    """
    names: set[str] = set()
    templates: set[str] = set()
    for package in _SEMCONV_PACKAGES:
        spec = importlib.util.find_spec(package)
        for location in (spec.submodule_search_locations or []) if spec else []:
            for path in Path(location).glob("*_attributes.py"):
                for constant, value in _SEMCONV_CONSTANT.findall(path.read_text(encoding="utf-8")):
                    (templates if constant.endswith("_TEMPLATE") else names).add(value)
    return frozenset(names), tuple(sorted(f"{template}." for template in templates))


def warm_semconv_registry() -> None:
    """Populate the semantic convention cache at startup so the first logged request never scans disk."""
    _semconv_registry()


def is_semconv_attribute(name: str) -> bool:
    """Return `True` when `name` matches an OpenTelemetry semantic convention attribute.

    Template attributes such as `http.request.header.<key>` match any
    non-empty `<key>` suffix after the prefix dot, such as
    `http.request.header.content-type`.
    """
    names, template_prefixes = _semconv_registry()
    if name in names:
        return True
    return any(len(name) > len(prefix) and name.startswith(prefix) for prefix in template_prefixes)


@functools.lru_cache(maxsize=4096)
def is_valid_attribute_name(name: str) -> bool:
    """Return `True` when `name` matches `i_ai.<service>.<property>` or an OpenTelemetry semantic convention."""
    return name in _SDK_LOG_ATTRIBUTES or bool(_I_AI_NAME.fullmatch(name)) or is_semconv_attribute(name)


def iai_attribute(name: str) -> str:
    """Build a custom attribute key under the `i_ai` namespace.

    Define attribute keys as module-level constants rather than inline inside
    `logger.info(..., extra={...})` calls. Invalid names then fail at import
    time in tests instead of raising inside a request.

    Args:
        name: Dot-separated `<service>.<property>` or deeper suffix in
            lowercase `snake_case`, without the `i_ai.` prefix.

    Returns:
        The full `i_ai.<name>` attribute key.

    Raises:
        ValueError: If `name` has fewer than two segments or is not lowercase
            `snake_case`.
    """
    full_name = f"{I_AI_NAMESPACE}.{name}"
    if not _I_AI_NAME.fullmatch(full_name):
        msg = (
            f"Invalid i_ai attribute {name!r}: use '<service>.<object>.<property>', "
            "lowercase, dot between levels, snake_case within a level (for example 'auth.token.key_id')."
        )
        raise ValueError(msg)
    return full_name


def validate_attributes(attributes: Mapping[str, Any]) -> dict[str, Any]:
    """Verify that every key in `attributes` follows OpenTelemetry or `i_ai` naming rules.

    Accepts OpenTelemetry semantic convention keys, including populated
    template keys such as `http.request.header.content-type`, and custom keys
    built with `iai_attribute`.

    Args:
        attributes: Attribute mapping to check before attaching to a log record
            or span.

    Returns:
        A shallow `dict` copy of `attributes` when all keys are valid.

    Raises:
        ValueError: Lists every key that is neither an OpenTelemetry semantic
            convention nor a valid `i_ai.<service>.<property>` key.
    """
    invalid = sorted(key for key in attributes if not is_valid_attribute_name(key))
    if invalid:
        msg = (
            f"Attribute names {invalid} are neither OpenTelemetry semantic conventions nor "
            f"'{I_AI_NAMESPACE}.<service>.<property>'. Use iai_attribute() to build custom names."
        )
        raise ValueError(msg)
    return dict(attributes)


def _is_strict_exempt_logger(log_record: ReadWriteLogRecord) -> bool:
    """Return `True` when `log_record` originates from a known third-party framework logger."""
    scope = getattr(log_record, "instrumentation_scope", None)
    logger_name = getattr(scope, "name", None) or ""
    return any(
        logger_name == prefix or logger_name.startswith(f"{prefix}.") for prefix in _THIRD_PARTY_STRICT_EXEMPT_LOGGERS
    )


class AttributeNamespaceProcessor(LogRecordProcessor):
    """Normalize and enforce attribute namespaces on every emitted SDK log record.

    `configure_otel` registers this processor ahead of export processors, so
    exporters receive only compliant keys and `I_DOT_AI_OTEL_STRICT_ATTRIBUTES=true`
    still catches invalid keys in tests when `OTEL_LOGS_EXPORTER=none`. Moves
    non-compliant keys under `i_ai.unnamespaced.*` rather than dropping them so
    log fields stay intact while warning once per offending key on `stderr`.
    Renames keys from known third-party framework loggers such as
    `django.request` instead of raising in strict mode.
    """

    def __init__(self) -> None:
        self._strict = env_flag(ENV_STRICT_ATTRIBUTES, default=False)

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        """Rewrite `aws_request_id` to `faas.invocation_id` and either reject or namespace invalid keys in place."""
        attributes = log_record.log_record.attributes
        if attributes:
            if _LAMBDA_REQUEST_ID_KEY in attributes:
                request_id = attributes[_LAMBDA_REQUEST_ID_KEY]
                del attributes[_LAMBDA_REQUEST_ID_KEY]  # type: ignore[attr-defined]
                # Skip empty request IDs recorded outside an active Lambda invocation.
                if request_id:
                    attributes[_INVOCATION_ID_ATTRIBUTE] = request_id  # type: ignore[index]
            invalid = [key for key in attributes if not is_valid_attribute_name(key)]
            if invalid:
                if self._strict and not _is_strict_exempt_logger(log_record):
                    validate_attributes(attributes)
                for key in invalid:
                    value = attributes[key]
                    # Delete the old key before inserting the renamed key so BoundedAttributes
                    # does not evict an unrelated oldest entry when the record is at its attribute limit.
                    del attributes[key]  # type: ignore[attr-defined]
                    attributes[f"{UNNAMESPACED_PREFIX}{key}"] = value  # type: ignore[index]
                    _warn_once(key)

    def force_flush(self, timeout_millis: int = 30000) -> bool:  # noqa: ARG002
        return True

    def shutdown(self) -> None:
        pass


def _warn_once(key: str) -> None:
    """Write a `stderr` warning for a non-compliant attribute `key` at most once per process."""
    if key in _warned_keys:
        return
    _warned_keys.add(key)
    sys.stderr.write(
        f"i_dot_ai_utilities.otel: log attribute {key!r} is not an OpenTelemetry semantic convention "
        f"or under '{I_AI_NAMESPACE}.'; exported as '{UNNAMESPACED_PREFIX}{key}'.\n"
    )


__all__ = [
    "ENV_STRICT_ATTRIBUTES",
    "I_AI_NAMESPACE",
    "UNNAMESPACED_PREFIX",
    "AttributeNamespaceProcessor",
    "iai_attribute",
    "is_semconv_attribute",
    "is_valid_attribute_name",
    "validate_attributes",
    "warm_semconv_registry",
]
