# mypy: disable-error-code="no-untyped-def"
"""Unit tests for ``middleware._headers.validate_request_id``.

This helper is the single line of defence between an attacker-controlled
``X-Request-ID`` header and the log context. Its failure mode is
security finding A3: accepting structural characters verbatim enables
log-injection, log-search hijack, and cross-tenant correlation poisoning.

Every rejection case below documents a specific attack class. Keep the
charset regex (``_REQUEST_ID_CHARSET_RE``) in lockstep with this test
file — if a reject case starts passing, the regex has been loosened and
the security property is gone.

Pure Python helper — no Django / OTel imports, no ``importorskip``.
"""

from __future__ import annotations

import pytest

from i_dot_ai_utilities.logging._limits import MAX_REQUEST_ID
from i_dot_ai_utilities.logging.middleware._headers import validate_request_id


class TestAcceptedInputs:
    def test_short_alphanumeric_passes_through(self):
        assert validate_request_id("abc-123") == "abc-123"

    def test_uuid_shape_accepted(self):
        uuid_hex = "0123456789abcdef0123456789abcdef"
        assert validate_request_id(uuid_hex) == uuid_hex

    def test_uuid_with_dashes_accepted(self):
        value = "0af76519-16cd-43dd-8448-eb211c80319c"
        assert validate_request_id(value) == value

    def test_cloudfront_style_base64_accepted(self):
        # X-Amz-Cf-Id style — opaque, mixed case, includes ``=`` + ``+`` + ``/``.
        value = "AbC123+/=_some-opaque-id"
        assert validate_request_id(value) == value

    def test_underscore_and_period_accepted(self):
        assert validate_request_id("job.42_retry.3") == "job.42_retry.3"

    def test_colon_accepted(self):
        # Colon is RFC 3986-reserved but appears in some tracing ids.
        assert validate_request_id("tenant:42") == "tenant:42"


class TestWhitespaceHandling:
    def test_leading_and_trailing_whitespace_stripped(self):
        assert validate_request_id("  abc-123  ") == "abc-123"

    def test_leading_tab_and_newline_stripped(self):
        assert validate_request_id("\t\nabc-123\t\n") == "abc-123"


class TestRejectedInputs:
    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(None, id="none"),
            pytest.param("", id="empty_string"),
            pytest.param("   ", id="whitespace_only"),
            pytest.param("\t\n\r", id="ws_chars_only"),
        ],
    )
    def test_empty_like_returns_none(self, value):
        assert validate_request_id(value) is None

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(12345, id="int"),
            pytest.param(12.5, id="float"),
            pytest.param(b"abc-123", id="bytes"),
            pytest.param(["abc"], id="list"),
            pytest.param({"id": "abc"}, id="dict"),
            pytest.param(object(), id="arbitrary_object"),
        ],
    )
    def test_non_string_returns_none(self, value):
        # Line 89 of _headers.py: ``not isinstance(value, str) -> None``.
        # Security: non-string inputs shouldn't even reach the charset
        # check; Python's ``str()`` on arbitrary objects would smuggle
        # attacker-chosen repr content into the charset gate.
        assert validate_request_id(value) is None

    @pytest.mark.parametrize(
        ("value", "attack_class"),
        [
            pytest.param("has space", "space-lets-grafana-split-queries", id="space"),
            pytest.param("has\ttab", "tab-injection", id="tab"),
            pytest.param("has\nnewline", "log-line-injection", id="newline"),
            pytest.param("has\rcr", "log-line-injection_cr", id="carriage_return"),
            pytest.param("has\x00null", "null-byte-injection", id="null_byte"),
            pytest.param("has\x1bescape", "ansi-escape-injection", id="escape_char"),
            pytest.param('has"quote', "json-structure-injection", id="double_quote"),
            pytest.param("has'quote", "sql-like-injection", id="single_quote"),
            pytest.param("has;semicolon", "log-field-separator", id="semicolon"),
            pytest.param("has,comma", "csv-separator", id="comma"),
            pytest.param("has<angle>", "html-injection", id="angle_brackets"),
            pytest.param("has|pipe", "shell-pipe", id="pipe"),
            pytest.param("has`backtick", "shell-substitution", id="backtick"),
            pytest.param("has$dollar", "variable-interpolation", id="dollar"),
            pytest.param("has\\backslash", "escape-injection", id="backslash"),
            pytest.param("has#hash", "yaml-comment", id="hash"),
            pytest.param("has(paren", "structure", id="paren"),
            pytest.param("has*star", "glob", id="asterisk"),
            pytest.param("has?question", "glob", id="question"),
            pytest.param("has{brace}", "templating", id="brace"),
            pytest.param("has@at", "email-like", id="at_sign"),
        ],
    )
    def test_rejects_disallowed_characters(self, value, attack_class):
        """Every character outside RFC 3986 unreserved + common base64 /
        JWT separators must be refused. ``attack_class`` documents the
        threat model each rejection closes so future maintainers
        understand why the regex is deliberately tight.
        """
        # ``attack_class`` is unused at runtime but makes grep-for-threat
        # easier when the charset regex is next revisited.
        _ = attack_class
        assert validate_request_id(value) is None


class TestTruncation:
    def test_truncates_to_default_max_length(self):
        result = validate_request_id("x" * 1000)
        assert result is not None
        assert len(result) == MAX_REQUEST_ID
        assert result == "x" * MAX_REQUEST_ID

    def test_explicit_max_length_is_honoured(self):
        assert validate_request_id("abcdef", max_length=3) == "abc"

    def test_at_limit_is_passed_through_unchanged(self):
        value = "x" * MAX_REQUEST_ID
        assert validate_request_id(value) == value

    def test_truncation_happens_before_charset_check(self):
        """Otherwise a payload longer than ``max_length`` containing
        structural characters only *after* the cut-off would be accepted
        based on its truncated tail. The helper truncates FIRST then
        validates, so the tail is what determines acceptance. A payload
        with banned chars in the first ``max_length`` bytes must still
        be rejected.
        """
        # Disallowed character at position 0 — truncation to 10 chars
        # cannot save it.
        assert validate_request_id(" bad payload", max_length=10) is None


class TestInjectionPayloadsRejected:
    """Regression tests for known-bad payloads. These are the exact
    shapes security reviewers have flagged; keep them here so reviewers
    can verify mitigation by name.
    """

    def test_quote_injection_payload_rejected(self):
        assert validate_request_id('victim-id" injected="pwned') is None

    def test_newline_log_splitting_payload_rejected(self):
        assert validate_request_id("victim-id\nFAKE_LOG_LINE request_id=attacker") is None

    def test_ansi_escape_payload_rejected(self):
        assert validate_request_id("\x1b[31mRED\x1b[0m") is None

    def test_loki_logql_injection_payload_rejected(self):
        # Loki label filters use {x="y"}; a value containing {} would
        # corrupt on-the-fly query construction if a downstream tool
        # ever interpolated it.
        assert validate_request_id('xyz"}|= "steal') is None
