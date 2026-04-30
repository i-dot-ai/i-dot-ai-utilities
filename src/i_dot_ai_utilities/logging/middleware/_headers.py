"""Canonical HTTP header names and size caps for the Django middleware.

Header name constants are in the canonical over-the-wire form (mixed case for
readability). Django's ``HttpRequest.headers`` lookup is case-insensitive, so
these values can be passed directly to ``request.headers.get(...)``.

Size caps live in ``i_dot_ai_utilities.logging._limits`` so the enricher and
middleware share a single source of truth; they are re-exported here for
backwards compatibility.

Pure Python module: no third-party imports, no side effects on import.
"""

from typing import Final

from i_dot_ai_utilities.logging._limits import (
    MAX_HEADER_VALUE,
    MAX_REQUEST_ID,
    MAX_URL_PATH,
    MAX_URL_QUERY,
)

# Correlation / trace propagation headers.
TRACEPARENT: Final[str] = "traceparent"
X_AMZN_TRACE_ID: Final[str] = "X-Amzn-Trace-Id"
X_REQUEST_ID: Final[str] = "X-Request-ID"

# Proxy / client metadata headers.
X_FORWARDED_FOR: Final[str] = "X-Forwarded-For"
USER_AGENT: Final[str] = "User-Agent"


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

__all__ = [
    "FORBIDDEN_HEADER_NAMES",
    "MAX_HEADER_VALUE",
    "MAX_REQUEST_ID",
    "MAX_URL_PATH",
    "MAX_URL_QUERY",
    "TRACEPARENT",
    "USER_AGENT",
    "X_AMZN_TRACE_ID",
    "X_FORWARDED_FOR",
    "X_REQUEST_ID",
]
