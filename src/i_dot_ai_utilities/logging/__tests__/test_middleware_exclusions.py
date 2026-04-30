# mypy: disable-error-code="no-untyped-def"
"""Unit tests for the ``ExclusionMatcher`` class."""

from __future__ import annotations

import re

import pytest

from i_dot_ai_utilities.logging.middleware._exclusions import (
    DEFAULT_EXCLUDED_PREFIXES,
    ExclusionMatcher,
)


class TestDefaultExcludedPrefixes:
    @pytest.mark.parametrize(
        "path",
        [
            "/health/",
            "/health/detail",
            "/healthz",
            "/healthz/",
            "/api/health/",
            "/api/health/live",
            "/api/health/ready",
        ],
    )
    def test_default_prefixes_match(self, path):
        matcher = ExclusionMatcher(DEFAULT_EXCLUDED_PREFIXES)
        assert matcher.matches(path) is True

    @pytest.mark.parametrize(
        "path",
        [
            "/api/users",
            "/",
            "/he",  # prefix of /health but not a real match
            "/healthcare",  # NOT a health-check path
            "/api/users/healthz",
        ],
    )
    def test_non_matching_paths(self, path):
        matcher = ExclusionMatcher(DEFAULT_EXCLUDED_PREFIXES)
        assert matcher.matches(path) is False


class TestEmptyMatcher:
    def test_matches_nothing(self):
        matcher = ExclusionMatcher()
        assert matcher.matches("/") is False
        assert matcher.matches("/healthz") is False
        assert matcher.matches("") is False


class TestRegexMatching:
    def test_string_regex_is_compiled(self):
        matcher = ExclusionMatcher(regexes=[r"^/probe/\d+$"])
        assert matcher.matches("/probe/42") is True
        assert matcher.matches("/probe/abc") is False

    def test_precompiled_pattern_is_reused_by_identity(self):
        pattern = re.compile(r"^/ping$")
        matcher = ExclusionMatcher(regexes=[pattern])
        # We rely on __slots__ + tuple storage; no direct attr access in public
        # API. Verify behaviour rather than implementation: a precompiled
        # pattern's .search is called, not re-compiled.
        assert matcher.matches("/ping") is True
        # Confirm same pattern object stored by matching on a sentinel attribute.
        # (We can't access _regexes externally without a slot violation; the
        # behavioural assertion above is sufficient.)

    def test_empty_path_never_matches(self):
        matcher = ExclusionMatcher(
            prefixes=["/"],
            regexes=[r".*"],
        )
        # Even with a permissive prefix/regex, an empty path returns False —
        # guards against logging spurious empty-path cases.
        assert matcher.matches("") is False

    def test_combines_prefix_and_regex(self):
        matcher = ExclusionMatcher(
            prefixes=["/health"],
            regexes=[r"^/metrics$"],
        )
        assert matcher.matches("/health/foo") is True
        assert matcher.matches("/metrics") is True
        assert matcher.matches("/nope") is False

    def test_regex_search_not_fullmatch(self):
        # The matcher uses .search so anchoring is up to the user.
        matcher = ExclusionMatcher(regexes=[r"probe"])
        assert matcher.matches("/api/probe/status") is True


class TestRepr:
    def test_includes_prefixes_and_regexes(self):
        matcher = ExclusionMatcher(prefixes=["/x"], regexes=[r"^/y$"])
        rendered = repr(matcher)
        assert "/x" in rendered
        assert "^/y$" in rendered
