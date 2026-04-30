"""Canonical HTTP header names, size caps, and request-id validation.

Header name constants are in the canonical over-the-wire form (mixed case for
readability). Django's ``HttpRequest.headers`` lookup is case-insensitive, so
these values can be passed directly to ``request.headers.get(...)``.

Size caps live in ``i_dot_ai_utilities.logging._limits`` so the middleware
has a single source of truth; they are re-exported here for backwards
compatibility.

``validate_request_id`` is kept local to this module (rather than pulled in
from the deleted ``_trace`` parser) because it is the single validation the
OTel-backed middleware still needs to perform on inbound correlation headers.

Pure Python module: no third-party imports, no side effects on import.
"""

import re
from typing import Final

from i_dot_ai_utilities.logging._limits import (
    MAX_HEADER_VALUE,
    MAX_REQUEST_ID,
    MAX_URL_PATH,
    MAX_URL_QUERY,
)

# Per-hop correlation header. Trace propagation (``traceparent`` /
# ``X-Amzn-Trace-Id``) is handled by the OpenTelemetry composite
# propagator installed via ``configure_otel_for_django``; the middleware
# only reads ``X-Request-ID`` directly to preserve an opaque upstream
# correlation id separately from the OTel span context.
X_REQUEST_ID: Final[str] = "X-Request-ID"


# Header names the middleware MUST refuse to log even when explicitly named
# in ``I_DOT_AI_LOGGING_HEADER_ALLOWLIST``. Constitution Art. 52 makes this
# guarantee unconditional — a misconfigured allowlist is a plausible
# operational mistake, and a denylist floor keeps the guarantee intact. Match
# is case-insensitive.
FORBIDDEN_HEADER_NAMES: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "cookie",
        "set-cookie",
        "proxy-authorization",
        "x-csrftoken",
        "x-csrf-token",
        "www-authenticate",
        "x-api-key",
    }
)


# RFC 3986 §2.3 unreserved characters plus the common separators seen in
# Envoy UUIDs, CloudFront opaque base64 identifiers, and JWT-shaped tokens.
# Deliberately restrictive: any character outside this class is rejected to
# prevent log-injection, log-search hijack, and correlation poisoning via
# attacker-controlled X-Request-ID values (see security finding A3).
_REQUEST_ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9._~=:+/\-]+$")


def validate_request_id(
    value: str | None,
    *,
    max_length: int = MAX_REQUEST_ID,
) -> str | None:
    """Validate and truncate an opaque ``X-Request-ID``-style identifier.

    Behaviour:

    - Returns the input stripped and truncated to ``max_length`` if non-empty
      AND composed entirely of RFC 3986 unreserved characters plus the
      common ``=``, ``+``, ``/`` separators seen in base64 identifiers.
    - Returns ``None`` for ``None``, empty, whitespace-only, or non-string
      values.
    - Returns ``None`` for values containing characters outside the
      permitted charset (e.g. whitespace, control bytes, quoting chars).
    - Never validates as UUID (Envoy uses UUID, CloudFront uses opaque
      base64, both are valid ``X-Request-ID`` / equivalents).
    - Never regenerates when present — upstream correlation depends on the
      verbatim value being preserved.

    Security (finding A3): accepting attacker-chosen ``X-Request-ID``
    values verbatim into log context enables log-injection and
    log-search hijack. The strict charset is the mitigation.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    truncated = stripped[:max_length]
    if _REQUEST_ID_CHARSET_RE.match(truncated) is None:
        return None
    return truncated


__all__ = [
    "FORBIDDEN_HEADER_NAMES",
    "MAX_HEADER_VALUE",
    "MAX_REQUEST_ID",
    "MAX_URL_PATH",
    "MAX_URL_QUERY",
    "X_REQUEST_ID",
    "validate_request_id",
]
