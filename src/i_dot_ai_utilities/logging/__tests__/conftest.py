# mypy: disable-error-code="no-untyped-def"
"""Shared pytest fixtures for the logging module tests.

Autouse fixture clears ``structlog.contextvars`` before and after every test
so bound context cannot leak between tests running on the same thread.
"""

import pytest
import structlog


@pytest.fixture(autouse=True)
def _clear_structlog_contextvars():
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()
