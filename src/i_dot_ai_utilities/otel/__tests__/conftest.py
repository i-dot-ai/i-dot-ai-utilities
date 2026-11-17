# ruff: noqa: SLF001 - these tests deliberately inspect private state and SDK internals.
"""Shared fixtures for native OTel tests."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

import pytest

from i_dot_ai_utilities.otel import _attributes, _configuration
from i_dot_ai_utilities.otel._instrumentation import uninstrument_installed_libraries
from i_dot_ai_utilities.otel._lambda import reset_inflight_flush

if TYPE_CHECKING:
    from collections.abc import Iterator


def reset_otel_state() -> None:
    """Undo everything ``configure_otel`` did in this process."""
    state = _configuration._state
    root_logger = logging.getLogger()
    if state.root_logging_handler is not None and state.root_logging_handler in root_logger.handlers:
        root_logger.removeHandler(state.root_logging_handler)
    if state.logger_provider is not None:
        with contextlib.suppress(Exception):
            state.logger_provider.shutdown()
    reset_inflight_flush()
    uninstrument_installed_libraries()
    _attributes._warned_keys.clear()
    _configuration._state = _configuration._State()


@pytest.fixture(autouse=True)
def _clean_otel_state() -> Iterator[None]:
    reset_otel_state()
    yield
    reset_otel_state()
