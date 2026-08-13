"""Small pure helpers: log-level selection, duration computation, truncation.

Kept in a dedicated module so they can be tested in isolation and reused
without pulling in any Django code.

``truncate`` has moved to ``i_dot_ai_utilities.logging._limits`` (so the
enricher can share it without crossing the middleware subpackage boundary).
It is re-exported here for backwards compatibility.
"""

from __future__ import annotations

from typing import Final

from i_dot_ai_utilities.logging._limits import truncate

# Canonical log level names (lowercase to match structlog's conventions).
LEVEL_INFO: Final[str] = "info"
LEVEL_WARNING: Final[str] = "warning"
LEVEL_ERROR: Final[str] = "error"

# HTTP status class boundaries, named so the level selector reads clearly.
_STATUS_CLIENT_ERROR_MIN: Final[int] = 400
_STATUS_SERVER_ERROR_MIN: Final[int] = 500
_STATUS_SERVER_ERROR_MAX_EXCLUSIVE: Final[int] = 600


def level_for_status(status_code: int) -> str:
    """Map an HTTP status code to a log level.

    - 2xx / 3xx -> ``info``
    - 4xx       -> ``warning``
    - 5xx       -> ``error``
    - anything else (unmapped 1xx or nonsense values) -> ``info`` as a safe default
    """
    if _STATUS_SERVER_ERROR_MIN <= status_code < _STATUS_SERVER_ERROR_MAX_EXCLUSIVE:
        return LEVEL_ERROR
    if _STATUS_CLIENT_ERROR_MIN <= status_code < _STATUS_SERVER_ERROR_MIN:
        return LEVEL_WARNING
    return LEVEL_INFO


def duration_ms(start_monotonic: float, end_monotonic: float) -> int:
    """Return an integer millisecond duration from two ``time.monotonic()`` samples.

    ``time.monotonic()`` is guaranteed non-decreasing; if a caller passes the
    samples in the wrong order we clamp to zero rather than returning a negative
    duration (which would corrupt downstream aggregations).
    """
    delta_ms = (end_monotonic - start_monotonic) * 1000.0
    if delta_ms < 0:
        return 0
    return int(delta_ms)


__all__ = [
    "LEVEL_ERROR",
    "LEVEL_INFO",
    "LEVEL_WARNING",
    "duration_ms",
    "level_for_status",
    "truncate",
]
