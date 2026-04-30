# mypy: disable-error-code="no-untyped-def"
"""Parametrised unit tests for the middleware's pure trace-header parsers.

These tests cover the spec edge cases from the research brief. No Django
imports — the parsers must be usable in non-Django consumers.
"""

from __future__ import annotations

import re

import pytest

from i_dot_ai_utilities.logging.middleware._headers import MAX_REQUEST_ID
from i_dot_ai_utilities.logging.middleware._trace import (
    parse_traceparent,
    parse_x_amzn_trace_id,
    resolve_trace_context,
    validate_request_id,
)

UUID_HEX_RE = re.compile(r"^[0-9a-f]{32}$")

# A valid W3C traceparent fixture. Trace id = 32 lowercase hex, span id = 16,
# flags = 01 (sampled).
VALID_TP_V00 = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
VALID_TRACE_ID = "a" * 32
VALID_SPAN_ID = "b" * 16


# ---------------------------------------------------------------------------
# parse_traceparent
# ---------------------------------------------------------------------------


class TestParseTraceparent:
    @pytest.mark.parametrize(
        ("value", "expected_trace_id"),
        [
            pytest.param(VALID_TP_V00, VALID_TRACE_ID, id="valid_v00_sampled"),
            pytest.param(
                "00-" + "a" * 32 + "-" + "b" * 16 + "-00",
                VALID_TRACE_ID,
                id="valid_v00_not_sampled",
            ),
        ],
    )
    def test_accepts_valid_v00(self, value, expected_trace_id):
        parsed = parse_traceparent(value)
        assert parsed is not None
        assert parsed["trace_id"] == expected_trace_id
        assert parsed["span_id"] == VALID_SPAN_ID
        assert parsed["trace_flags"] in ("00", "01")

    def test_masks_trace_flags_to_low_byte(self):
        # flags = ff would normally be suspicious, but bit 0 is set and the
        # parser must preserve the full masked byte rather than equality-check
        # against "01".
        parsed = parse_traceparent("00-" + "a" * 32 + "-" + "b" * 16 + "-ff")
        assert parsed is not None
        assert parsed["trace_flags"] == "ff"

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(None, id="none"),
            pytest.param("", id="empty_string"),
            pytest.param("garbage", id="garbage"),
            pytest.param("00-tooshort", id="too_few_parts"),
            pytest.param("00", id="just_version"),
        ],
    )
    def test_rejects_obviously_malformed(self, value):
        assert parse_traceparent(value) is None

    def test_rejects_forbidden_version_ff(self):
        value = "ff-" + "a" * 32 + "-" + "b" * 16 + "-01"
        assert parse_traceparent(value) is None

    def test_rejects_uppercase_hex(self):
        # W3C spec §3.2.2 mandates lowercase hex.
        value = "00-" + "A" * 32 + "-" + "b" * 16 + "-01"
        assert parse_traceparent(value) is None

    def test_rejects_all_zero_trace_id(self):
        # W3C §3.2.2.3
        value = "00-" + "0" * 32 + "-" + "b" * 16 + "-01"
        assert parse_traceparent(value) is None

    def test_rejects_all_zero_span_id(self):
        # W3C §3.2.2.4
        value = "00-" + "a" * 32 + "-" + "0" * 16 + "-01"
        assert parse_traceparent(value) is None

    def test_rejects_wrong_length_trace_id(self):
        # 31 chars instead of 32.
        value = "00-" + "a" * 31 + "-" + "b" * 16 + "-01"
        assert parse_traceparent(value) is None

    def test_rejects_wrong_length_span_id(self):
        # 15 chars instead of 16.
        value = "00-" + "a" * 32 + "-" + "b" * 15 + "-01"
        assert parse_traceparent(value) is None

    def test_rejects_wrong_length_flags(self):
        # 1 hex char instead of 2.
        value = "00-" + "a" * 32 + "-" + "b" * 16 + "-1"
        assert parse_traceparent(value) is None

    def test_rejects_v00_with_trailing_data(self):
        # Version 00 is strict: exactly 55 chars total, 4 segments.
        value = VALID_TP_V00 + "-extra"
        assert parse_traceparent(value) is None

    def test_higher_version_best_effort_parse(self):
        # W3C §3.2.4: unknown higher versions should be parsed best-effort
        # from the first four segments; trailing data is ignored.
        value = "01-" + "a" * 32 + "-" + "b" * 16 + "-01-futurefield"
        parsed = parse_traceparent(value)
        assert parsed is not None
        assert parsed["trace_id"] == VALID_TRACE_ID
        assert parsed["span_id"] == VALID_SPAN_ID

    def test_multivalued_header_takes_first(self):
        # HTTP allows repeated fields; Django exposes them joined by commas.
        value = VALID_TP_V00 + ", garbage-second-value"
        parsed = parse_traceparent(value)
        assert parsed is not None
        assert parsed["trace_id"] == VALID_TRACE_ID

    def test_leading_whitespace_tolerated(self):
        parsed = parse_traceparent("   " + VALID_TP_V00)
        assert parsed is not None

    def test_non_string_input_returns_none(self):
        # The parser must tolerate wrong types without raising.
        assert parse_traceparent(b"bytes") is None  # type: ignore[arg-type]
        assert parse_traceparent(12345) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# parse_x_amzn_trace_id
# ---------------------------------------------------------------------------


class TestParseXAmznTraceId:
    def test_root_only(self):
        result = parse_x_amzn_trace_id("Root=1-5759e988-bd862e3fe1be46a994272793")
        assert result == {"amzn_trace_root": "1-5759e988-bd862e3fe1be46a994272793"}

    def test_root_parent_sampled(self):
        result = parse_x_amzn_trace_id("Root=1-abc-def;Parent=53995c3f42cd8ad8;Sampled=1")
        assert result == {"amzn_trace_root": "1-abc-def"}

    def test_reordered_fields(self):
        # Field order per AWS docs is NOT fixed — parser must cope.
        result = parse_x_amzn_trace_id("Parent=53995c3f42cd8ad8;Sampled=1;Root=1-abc-def")
        assert result == {"amzn_trace_root": "1-abc-def"}

    def test_self_field_is_not_trace_id(self):
        # ALB-only: Self=... is added by the LB; Root=... is the trace id.
        # Absence of Root must yield None, even if Self is present.
        result = parse_x_amzn_trace_id("Self=1-abc-def;Parent=xxx")
        assert result is None

    def test_self_with_root_extracts_root(self):
        result = parse_x_amzn_trace_id("Self=1-bbb-aaa;Root=1-ccc-ddd;Sampled=1")
        assert result == {"amzn_trace_root": "1-ccc-ddd"}

    def test_lambda_lineage_field_ignored(self):
        # Lambda appends Lineage=... which must not interfere with Root=.
        result = parse_x_amzn_trace_id("Root=1-abc-def;Lineage=a:b:c;Sampled=1")
        assert result == {"amzn_trace_root": "1-abc-def"}

    def test_missing_root_returns_none(self):
        assert parse_x_amzn_trace_id("Parent=xxx;Sampled=0") is None

    def test_empty_root_value_returns_none(self):
        assert parse_x_amzn_trace_id("Root=;Parent=xxx") is None

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(None, id="none"),
            pytest.param("", id="empty_string"),
            pytest.param(";;;", id="only_separators"),
            pytest.param("no_equals_sign", id="no_key_value_pair"),
        ],
    )
    def test_degenerate_input(self, value):
        assert parse_x_amzn_trace_id(value) is None

    def test_whitespace_around_fields_tolerated(self):
        result = parse_x_amzn_trace_id("  Root = 1-abc-def ; Sampled = 1 ")
        assert result == {"amzn_trace_root": "1-abc-def"}

    def test_not_root_prefix_does_not_match(self):
        # A field whose name merely contains "Root" must not false-positive.
        assert parse_x_amzn_trace_id("NotRoot=1-x;Parent=y") is None


# ---------------------------------------------------------------------------
# validate_request_id
# ---------------------------------------------------------------------------


class TestValidateRequestId:
    def test_passthrough_small(self):
        assert validate_request_id("abc-123") == "abc-123"

    def test_truncation_to_max(self):
        result = validate_request_id("x" * 1000)
        assert result is not None
        assert len(result) == MAX_REQUEST_ID

    def test_explicit_max_length(self):
        assert validate_request_id("abcdef", max_length=3) == "abc"

    def test_strips_whitespace(self):
        assert validate_request_id("  foo  ") == "foo"

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(None, id="none"),
            pytest.param("", id="empty"),
            pytest.param("   ", id="whitespace_only"),
        ],
    )
    def test_empty_like_returns_none(self, value):
        assert validate_request_id(value) is None

    def test_non_string_returns_none(self):
        assert validate_request_id(12345) is None  # type: ignore[arg-type]

    def test_does_not_require_uuid(self):
        # CloudFront's X-Amz-Cf-Id is opaque base64-ish; must not be rejected.
        result = validate_request_id("AbC123+/=_weirdbase64ish")
        assert result == "AbC123+/=_weirdbase64ish"

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param("has space", id="contains_space"),
            pytest.param("has\ttab", id="contains_tab"),
            pytest.param("has\nnewline", id="contains_newline"),
            pytest.param("has\x00null", id="contains_null_byte"),
            pytest.param('has"quote', id="contains_double_quote"),
            pytest.param("has'quote", id="contains_single_quote"),
            pytest.param("has;semicolon", id="contains_semicolon"),
            pytest.param("has,comma", id="contains_comma"),
            pytest.param("has<angle>", id="contains_angle_brackets"),
            pytest.param("has|pipe", id="contains_pipe"),
        ],
    )
    def test_rejects_disallowed_characters(self, value):
        # Security: values containing characters outside the RFC 3986
        # unreserved + common base64/JWT set are refused to prevent
        # log-injection and correlation-hijack via attacker-controlled
        # X-Request-ID payloads (finding A3).
        assert validate_request_id(value) is None


# ---------------------------------------------------------------------------
# resolve_trace_context
# ---------------------------------------------------------------------------


class TestResolveTraceContext:
    def test_traceparent_wins_when_all_present(self):
        ctx = resolve_trace_context(
            traceparent=VALID_TP_V00,
            amzn="Root=1-abc-def",
            req_id="upstream-req-id",
        )
        assert ctx["trace_id"] == VALID_TRACE_ID
        assert ctx["span_id"] == VALID_SPAN_ID
        assert ctx["trace_flags"] == "01"
        assert ctx["trace_id_source"] == "traceparent"
        # upstream request id is preserved separately, not used as trace_id.
        assert ctx["upstream_request_id"] == "upstream-req-id"
        assert ctx["amzn_trace_root"] == "1-abc-def"
        # Fresh per-hop request_id is always generated.
        assert UUID_HEX_RE.match(ctx["request_id"])
        assert ctx["request_id"] != "upstream-req-id"

    def test_amzn_wins_when_traceparent_missing(self):
        ctx = resolve_trace_context(
            traceparent=None,
            amzn="Root=1-abc-def;Sampled=1",
            req_id="upstream",
        )
        assert ctx["trace_id"] == "1-abc-def"
        assert ctx["trace_id_source"] == "amzn"
        assert "span_id" not in ctx  # only traceparent binds span_id
        assert ctx["amzn_trace_root"] == "1-abc-def"
        assert ctx["upstream_request_id"] == "upstream"

    def test_request_id_wins_when_hex_shaped(self):
        # Hex/UUID shape is required for the X-Request-ID -> trace_id
        # promotion. A 32-hex Envoy-style UUID qualifies.
        hex_id = "0123456789abcdef0123456789abcdef"
        ctx = resolve_trace_context(
            traceparent=None,
            amzn=None,
            req_id=hex_id,
        )
        assert ctx["trace_id"] == hex_id
        assert ctx["trace_id_source"] == "request_id"
        assert "span_id" not in ctx
        assert "amzn_trace_root" not in ctx
        assert ctx["upstream_request_id"] == hex_id

    def test_non_hex_request_id_does_not_win_trace_id_slot(self):
        # Security (A3): an opaque non-hex X-Request-ID must NOT be promoted
        # to trace_id. It is preserved as upstream_request_id but trace_id
        # is synthesised fresh so cross-tenant correlation graphs cannot
        # be poisoned by attacker-chosen identifiers.
        ctx = resolve_trace_context(
            traceparent=None,
            amzn=None,
            req_id="job-42-retry-3",
        )
        assert ctx["upstream_request_id"] == "job-42-retry-3"
        assert ctx["trace_id"] != "job-42-retry-3"
        assert UUID_HEX_RE.match(ctx["trace_id"])
        assert ctx["trace_id_source"] == "synthetic"
        assert "span_id" not in ctx

    def test_generated_uuid_when_all_absent(self):
        ctx = resolve_trace_context(None, None, None)
        assert UUID_HEX_RE.match(ctx["trace_id"])
        assert ctx["trace_id_source"] == "synthetic"
        assert "span_id" not in ctx
        assert "amzn_trace_root" not in ctx
        assert "upstream_request_id" not in ctx
        assert UUID_HEX_RE.match(ctx["request_id"])
        assert ctx["request_id"] != ctx["trace_id"]

    def test_malformed_traceparent_falls_through_to_amzn(self):
        ctx = resolve_trace_context(
            traceparent="ff-" + "a" * 32 + "-" + "b" * 16 + "-01",  # forbidden ver
            amzn="Root=1-fallback",
            req_id=None,
        )
        assert ctx["trace_id"] == "1-fallback"
        assert ctx["trace_id_source"] == "amzn"

    def test_malformed_amzn_with_non_hex_request_id_synthesises(self):
        # No valid trace header + non-hex X-Request-ID = synthetic trace_id.
        ctx = resolve_trace_context(
            traceparent=None,
            amzn="Parent=xxx;Self=yyy",  # no Root=
            req_id="the-request-id",
        )
        assert ctx["upstream_request_id"] == "the-request-id"
        assert ctx["trace_id"] != "the-request-id"
        assert UUID_HEX_RE.match(ctx["trace_id"])
        assert ctx["trace_id_source"] == "synthetic"

    def test_malformed_amzn_with_hex_request_id_uses_request_id(self):
        hex_id = "abcdef0123456789abcdef0123456789"
        ctx = resolve_trace_context(
            traceparent=None,
            amzn="Parent=xxx;Self=yyy",  # no Root=
            req_id=hex_id,
        )
        assert ctx["trace_id"] == hex_id
        assert ctx["trace_id_source"] == "request_id"

    def test_request_id_always_fresh_per_call(self):
        ctx1 = resolve_trace_context(None, None, None)
        ctx2 = resolve_trace_context(None, None, None)
        assert ctx1["request_id"] != ctx2["request_id"]
        assert ctx1["trace_id"] != ctx2["trace_id"]

    def test_injection_payload_in_request_id_is_rejected(self):
        # Security (A3): payloads containing spaces, quotes, semicolons, or
        # other structural characters must never reach the log context. The
        # inbound header is scrubbed entirely (upstream_request_id omitted)
        # and trace_id is synthesised.
        ctx = resolve_trace_context(
            traceparent=None,
            amzn=None,
            req_id='victim-id" injected="pwned',
        )
        assert "upstream_request_id" not in ctx
        assert UUID_HEX_RE.match(ctx["trace_id"])
        assert ctx["trace_id_source"] == "synthetic"
