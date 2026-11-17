"""Attribute naming rules: OpenTelemetry semantic conventions first, otherwise the ``i_ai`` namespace."""

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

_SEGMENT: Final[str] = r"[a-z][a-z0-9_]*"
# ``i_ai.<service>.<property>`` at minimum; deeper ``<object>.<property>`` levels are allowed.
_I_AI_NAME: Final[re.Pattern[str]] = re.compile(rf"{I_AI_NAMESPACE}(\.{_SEGMENT}){{2,}}")
_SUFFIX: Final[re.Pattern[str]] = re.compile(rf"{_SEGMENT}(\.{_SEGMENT})*")
_SEMCONV_CONSTANT: Final[re.Pattern[str]] = re.compile(r'^([A-Z][A-Z0-9_]*): Final = "([^"]+)"', re.MULTILINE)
_SEMCONV_PACKAGES: Final[tuple[str, ...]] = (
    "opentelemetry.semconv.attributes",
    "opentelemetry.semconv._incubating.attributes",
)

_warned_keys: set[str] = set()


@functools.cache
def _semconv_registry() -> tuple[frozenset[str], tuple[str, ...]]:
    """Return ``(attribute names, template prefixes)`` from the installed semantic conventions package.

    The constants are read from source rather than imported: importing all
    ~80 modules costs ~300 ms of cold start, reading them costs ~5 ms. A unit
    test checks both methods agree, so a format change in a new release fails CI.
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


def is_semconv_attribute(name: str) -> bool:
    """Return True when ``name`` is in the OpenTelemetry attribute registry.

    Template attributes such as ``http.request.header.<key>`` match any
    lowercase suffix.
    """
    names, template_prefixes = _semconv_registry()
    if name in names:
        return True
    return any(name.startswith(prefix) and _SUFFIX.fullmatch(name[len(prefix) :]) for prefix in template_prefixes)


@functools.lru_cache(maxsize=4096)
def is_valid_attribute_name(name: str) -> bool:
    """Return True for a semantic convention name or a well-formed ``i_ai.<service>.<property>`` name."""
    return bool(_I_AI_NAME.fullmatch(name)) or is_semconv_attribute(name)


def iai_attribute(name: str) -> str:
    """Return ``i_ai.<name>``, raising ``ValueError`` unless ``name`` is ``<service>.<property>`` or deeper.

    >>> iai_attribute("auth.decision.reason")
    'i_ai.auth.decision.reason'
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
    """Return ``attributes`` as a dict, raising ``ValueError`` listing every non-compliant key."""
    invalid = sorted(key for key in attributes if not is_valid_attribute_name(key))
    if invalid:
        msg = (
            f"Attribute names {invalid} are neither OpenTelemetry semantic conventions nor "
            f"'{I_AI_NAMESPACE}.<service>.<property>'. Use iai_attribute() to build custom names."
        )
        raise ValueError(msg)
    return dict(attributes)


class AttributeNamespaceProcessor(LogRecordProcessor):
    """Move non-compliant log attribute keys under ``i_ai.unnamespaced.`` before export.

    Runs ahead of ``delegate`` so the exporter only ever sees compliant keys.
    Keys are renamed rather than dropped so no data is lost, and each offending
    key is reported once on stderr. Set ``I_DOT_AI_OTEL_STRICT_ATTRIBUTES=true``
    to raise ``ValueError`` from the logging call instead, for tests and CI.
    """

    def __init__(self, delegate: LogRecordProcessor) -> None:
        self._delegate = delegate
        self._strict = env_flag(ENV_STRICT_ATTRIBUTES, default=False)

    @property
    def delegate(self) -> LogRecordProcessor:
        return self._delegate

    def on_emit(self, log_record: ReadWriteLogRecord) -> None:
        attributes = log_record.log_record.attributes
        if attributes:
            invalid = [key for key in attributes if not is_valid_attribute_name(key)]
            if invalid:
                if self._strict:
                    validate_attributes(attributes)
                for key in invalid:
                    value = attributes[key]
                    # Delete first: BoundedAttributes evicts the oldest key when a set exceeds its limit.
                    del attributes[key]  # type: ignore[attr-defined]
                    attributes[f"{UNNAMESPACED_PREFIX}{key}"] = value  # type: ignore[index]
                    _warn_once(key)
        self._delegate.on_emit(log_record)

    def force_flush(self, timeout_millis: int = 30000) -> bool:
        return bool(self._delegate.force_flush(timeout_millis))

    def shutdown(self) -> None:
        self._delegate.shutdown()


def _warn_once(key: str) -> None:
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
]
