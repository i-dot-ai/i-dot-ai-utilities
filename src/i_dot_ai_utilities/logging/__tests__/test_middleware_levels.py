# mypy: disable-error-code="no-untyped-def"
"""Unit tests for ``_levels.py`` — level selection, duration, truncation."""

from __future__ import annotations

import pytest

from i_dot_ai_utilities.logging.middleware._levels import (
    duration_ms,
    level_for_status,
    truncate,
)


class TestLevelForStatus:
    @pytest.mark.parametrize(
        "status",
        [200, 201, 204, 299, 300, 301, 302, 399],
    )
    def test_info_for_2xx_3xx(self, status):
        assert level_for_status(status) == "info"

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 499])
    def test_warning_for_4xx(self, status):
        assert level_for_status(status) == "warning"

    @pytest.mark.parametrize("status", [500, 502, 503, 504, 599])
    def test_error_for_5xx(self, status):
        assert level_for_status(status) == "error"

    @pytest.mark.parametrize("status", [0, 100, 199, 600, 999])
    def test_unmapped_defaults_to_info(self, status):
        # Safe default: "don't flood error channels with weird values".
        assert level_for_status(status) == "info"


class TestDurationMs:
    def test_simple_delta(self):
        # Use 0.006 to sidestep floating-point precision on 0.005.
        assert duration_ms(1.0, 1.006) == 6

    def test_zero_delta(self):
        assert duration_ms(10.0, 10.0) == 0

    def test_sub_millisecond_truncates_to_zero(self):
        assert duration_ms(1.0, 1.0004) == 0

    def test_negative_delta_clamps_to_zero(self):
        # Defensive: callers should pass (start, end) in order, but if they
        # don't, don't corrupt aggregations with negative values.
        assert duration_ms(2.0, 1.0) == 0

    def test_large_delta(self):
        # 3.5 seconds.
        assert duration_ms(0.0, 3.5) == 3500


class TestTruncate:
    def test_shorter_than_limit_unchanged(self):
        assert truncate("hi", 10) == "hi"

    def test_equal_to_limit_unchanged(self):
        assert truncate("hello", 5) == "hello"

    def test_longer_than_limit_truncated(self):
        assert truncate("hello world", 5) == "hello"

    def test_none_passes_through(self):
        assert truncate(None, 10) is None

    def test_zero_limit(self):
        assert truncate("foo", 0) == ""

    def test_negative_limit_treated_as_zero(self):
        assert truncate("foo", -5) == ""
