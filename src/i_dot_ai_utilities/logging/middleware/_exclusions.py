"""Path-exclusion matcher for the Django logging middleware.

Supports two match modes:

- Prefix matching (default) — O(n) string comparisons, predictable, no regex.
- Regex matching (opt-in) — compiled once at construction time.

Prefix defaults are health-check endpoints we observed across i.AI services.
Pure Python: no Django import.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable


# Health-check prefixes that all i.AI services should exclude by default.
# Consumers may override entirely via the ``I_DOT_AI_LOGGING_EXCLUDED_PREFIXES``
# setting; these are only the defaults.
DEFAULT_EXCLUDED_PREFIXES: Final[tuple[str, ...]] = (
    "/health/",
    "/healthz",
    "/api/health/",
    "/api/health/live",
    "/api/health/ready",
)


class ExclusionMatcher:
    """Test paths against prefix / regex exclusions.

    Prefixes use a plain ``str.startswith`` check (cheap, deterministic).
    Regex patterns are accepted either as strings (compiled once in
    ``__init__``) or as already-compiled ``re.Pattern`` objects (passed through
    by identity). A match by either mechanism is a full exclusion.
    """

    __slots__ = ("_prefixes", "_regexes")

    def __init__(
        self,
        prefixes: Iterable[str] = (),
        regexes: Iterable[str | re.Pattern[str]] = (),
    ) -> None:
        self._prefixes: tuple[str, ...] = tuple(prefixes)
        compiled: list[re.Pattern[str]] = []
        for pattern in regexes:
            if isinstance(pattern, re.Pattern):
                compiled.append(pattern)
            else:
                compiled.append(re.compile(pattern))
        self._regexes: tuple[re.Pattern[str], ...] = tuple(compiled)

    def matches(self, path: str) -> bool:
        """Return ``True`` iff ``path`` is excluded by prefix or regex."""
        if not path:
            return False
        for prefix in self._prefixes:
            if path.startswith(prefix):
                return True
        return any(regex.search(path) for regex in self._regexes)

    def __repr__(self) -> str:
        return f"ExclusionMatcher(prefixes={self._prefixes!r}, regexes={[r.pattern for r in self._regexes]!r})"
