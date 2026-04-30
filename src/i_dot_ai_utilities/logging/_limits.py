"""Shared size caps and truncation helper.

Centralised here so both the middleware (``middleware/*``) and the enricher
(``enrichers/django_enricher``) can apply the same ingest-time bounds without
reaching across subpackage boundaries into the middleware module.

Pure Python, no third-party imports, no side effects on import — safe to
import in non-Django consumers (verified by ``test_optional_django_import``).
"""

from __future__ import annotations

from typing import Final

# Size caps. Deliberately small: log ingestion is vastly cheaper when fields
# are bounded, and uncapped values turn pathological inputs (multi-MB
# User-Agent strings, absurd query strings) into multi-MB log lines.
MAX_HEADER_VALUE: Final[int] = 512
MAX_URL_PATH: Final[int] = 2048
MAX_URL_QUERY: Final[int] = 1024
MAX_REQUEST_ID: Final[int] = 200


def truncate(value: str | None, limit: int) -> str | None:
    """Truncate a string to ``limit`` chars, passing ``None`` through unchanged.

    No truncation marker is appended — we want the field to stay bounded even
    if consumers concatenate it with other data downstream. The caller can add
    a marker if it wants to indicate truncation.
    """
    if value is None:
        return None
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit]
