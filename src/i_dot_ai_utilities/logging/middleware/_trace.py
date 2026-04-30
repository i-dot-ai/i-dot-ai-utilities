"""Pure-Python parsers for distributed-tracing headers.

Implements three parsers plus a precedence resolver:

- ``parse_traceparent`` — W3C Trace Context Level 1.
- ``parse_x_amzn_trace_id`` — AWS X-Ray tracing header.
- ``validate_request_id`` — opaque X-Request-ID / X-Amz-Cf-Id passthrough.
- ``resolve_trace_context`` — merges the above via a defined precedence ladder
  and always returns a per-hop ``request_id``.

All functions are exception-safe (never raise on malformed input) and have no
Django dependency. Validation is deliberately strict on the W3C ``traceparent``
spec (lowercase hex only, reject all-zero ids, reject version ``ff``, best-effort
parse higher future versions) and strict-but-liberal on opaque ``X-Request-ID``
values: the charset is constrained to RFC 3986 unreserved + common base64/JWT
characters so log-search hijack via attacker-chosen identifiers is not possible
(see security review finding A3).
"""

from __future__ import annotations

import re
import uuid
from typing import Final

from i_dot_ai_utilities.logging.middleware._headers import MAX_REQUEST_ID

# --- traceparent parsing ----------------------------------------------------

# W3C Trace Context §3.2.2: lowercase hex only.
_LOWER_HEX_RE = re.compile(r"^[0-9a-f]+$")

# Version 00 has four dash-separated segments of fixed lengths:
#   00 (2) - trace-id (32) - parent-id (16) - trace-flags (2) = 55 chars.
_TRACEPARENT_V00_LEN = 55
_TRACE_ID_LEN = 32
_SPAN_ID_LEN = 16
_FLAGS_LEN = 2
_VERSION_LEN = 2
_MIN_TRACEPARENT_SEGMENTS: Final[int] = 4

_ALL_ZERO_TRACE_ID = "0" * _TRACE_ID_LEN
_ALL_ZERO_SPAN_ID = "0" * _SPAN_ID_LEN

# The only version explicitly forbidden by the spec.
_FORBIDDEN_VERSION = "ff"


def _is_valid_version(version: str) -> bool:
    return len(version) == _VERSION_LEN and _LOWER_HEX_RE.match(version) is not None and version != _FORBIDDEN_VERSION


def _is_valid_trace_id(trace_id: str) -> bool:
    return (
        len(trace_id) == _TRACE_ID_LEN and _LOWER_HEX_RE.match(trace_id) is not None and trace_id != _ALL_ZERO_TRACE_ID
    )


def _is_valid_span_id(span_id: str) -> bool:
    return len(span_id) == _SPAN_ID_LEN and _LOWER_HEX_RE.match(span_id) is not None and span_id != _ALL_ZERO_SPAN_ID


def _parse_flags(flags: str) -> str | None:
    if len(flags) != _FLAGS_LEN or not _LOWER_HEX_RE.match(flags):
        return None
    try:
        return f"{int(flags, 16) & 0xFF:02x}"
    except ValueError:
        return None


def parse_traceparent(value: str | None) -> dict[str, str] | None:  # noqa: PLR0911
    """Parse a W3C ``traceparent`` header.

    Returns ``{"trace_id", "span_id", "trace_flags"}`` on success, or ``None`` on
    any malformed input. Never raises.

    Validation follows W3C Trace Context Level 1 (Rec. 2021-11-23):

    - Lowercase hex required (§3.2.2).
    - All-zero ``trace-id`` or ``parent-id`` invalid (§3.2.2.3 / §3.2.2.4).
    - Version ``ff`` forbidden (§3.2.2.1).
    - Higher future versions: best-effort parse of the first 55 chars,
      trailing data tolerated (§3.2.4).
    - ``trace-flags`` byte masked to the low 8 bits (§3.2.2.5).
    """
    if not value or not isinstance(value, str):
        return None

    # Multiple traceparent headers may arrive as a comma-joined string per
    # RFC 9110 §5.3. Take the first value and discard the rest; concatenating
    # would never parse.
    first = value.split(",", 1)[0].strip()
    if not first:
        return None

    parts = first.split("-")
    if len(parts) < _MIN_TRACEPARENT_SEGMENTS:
        return None

    version, trace_id, span_id, flags = parts[0], parts[1], parts[2], parts[3]

    if not _is_valid_version(version):
        return None

    # Version 00: strict — must be exactly 4 parts of exactly 55 chars total.
    # Higher versions: best-effort — extract the first four fields, ignore the
    # rest (may carry additional fields in future).
    if version == "00" and (len(parts) != _MIN_TRACEPARENT_SEGMENTS or len(first) != _TRACEPARENT_V00_LEN):
        return None

    if not _is_valid_trace_id(trace_id):
        return None
    if not _is_valid_span_id(span_id):
        return None

    masked_flags = _parse_flags(flags)
    if masked_flags is None:
        return None

    return {
        "trace_id": trace_id,
        "span_id": span_id,
        "trace_flags": masked_flags,
    }


# --- X-Amzn-Trace-Id parsing ------------------------------------------------


def parse_x_amzn_trace_id(value: str | None) -> dict[str, str] | None:
    """Parse an AWS X-Ray ``X-Amzn-Trace-Id`` header.

    Returns ``{"amzn_trace_root": "<Root value incl. 1- prefix>"}`` on success,
    or ``None`` if ``Root=`` is absent / malformed / empty. Never raises.

    AWS spec (X-Ray Developer Guide, ALB request tracing):

    - Fields are separated by ``;`` in arbitrary order.
    - Each field is ``Key=Value``.
    - ``Root=1-<8-hex epoch>-<24-hex random>`` is the trace id; the ``1-``
      version prefix is preserved when propagating downstream.
    - ``Self=`` is a load-balancer-hop id, *not* the trace id, and must be
      ignored here.
    - ``Lineage=``, ``Parent=``, custom keys: tolerated but unused by us.
    - Values are not URL-decoded (they are already in wire format).
    """
    if not value or not isinstance(value, str):
        return None

    root_value: str | None = None
    for raw_field in value.split(";"):
        field = raw_field.strip()
        if not field:
            continue
        key, sep, val = field.partition("=")
        if not sep:
            # Malformed "key" with no "=" — tolerate, skip.
            continue
        key = key.strip()
        val = val.strip()
        if key == "Root" and val:
            root_value = val
            break

    if root_value is None:
        return None

    return {"amzn_trace_root": root_value}


# --- X-Request-ID validation ------------------------------------------------

# RFC 3986 §2.3 unreserved characters plus the common separators seen in
# Envoy UUIDs, CloudFront opaque base64 identifiers, and JWT-shaped tokens.
# Deliberately restrictive: any character outside this class is rejected to
# prevent log-injection, log-search hijack, and correlation poisoning via
# attacker-controlled X-Request-ID values (see security finding A3).
_REQUEST_ID_CHARSET_RE = re.compile(r"^[A-Za-z0-9._~=:+/\-]+$")

# Hex/UUID shape: lowercase or uppercase hex, optionally dash-separated,
# between 8 and 64 characters. Used to decide whether an inbound
# X-Request-ID is shaped like a legitimate trace identifier and therefore
# safe to promote to the trace_id slot in the precedence ladder.
_HEX_OR_UUID_RE = re.compile(r"^[0-9a-fA-F\-]{8,64}$")


def validate_request_id(
    value: str | None,
    *,
    max_length: int = MAX_REQUEST_ID,
) -> str | None:
    """Validate and truncate an opaque ``X-Request-ID``-style identifier.

    Behaviour:

    - Returns the input stripped and truncated to ``max_length`` if non-empty
      AND composed entirely of RFC 3986 unreserved characters plus the
      common ``=``, ``+``, ``/`` separators seen in base64 identifiers.
    - Returns ``None`` for ``None``, empty, whitespace-only, or non-string
      values.
    - Returns ``None`` for values containing characters outside the
      permitted charset (e.g. whitespace, control bytes, quoting chars).
    - Never validates as UUID (Envoy uses UUID, CloudFront uses opaque
      base64, both are valid ``X-Request-ID`` / equivalents).
    - Never regenerates when present — upstream correlation depends on the
      verbatim value being preserved.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    truncated = stripped[:max_length]
    if _REQUEST_ID_CHARSET_RE.match(truncated) is None:
        return None
    return truncated


# --- Precedence resolver ----------------------------------------------------

# Provenance markers for the trace_id slot. Emitted as ``trace_id_source`` so
# security-minded log queries can distinguish trusted (W3C / AWS parsed)
# trace identifiers from opaque inbound values or locally synthesised UUIDs.
TRACE_ID_SOURCE_TRACEPARENT: Final[str] = "traceparent"
TRACE_ID_SOURCE_AMZN: Final[str] = "amzn"
TRACE_ID_SOURCE_REQUEST_ID: Final[str] = "request_id"
TRACE_ID_SOURCE_SYNTHETIC: Final[str] = "synthetic"


def resolve_trace_context(
    traceparent: str | None,
    amzn: str | None,
    req_id: str | None,
) -> dict[str, str]:
    """Merge inbound correlation headers into a flat context dict.

    Precedence for ``trace_id``:

    1. Valid W3C ``traceparent``.
    2. ``Root=`` from ``X-Amzn-Trace-Id``.
    3. Validated ``X-Request-ID`` ONLY when it matches a hex/UUID shape
       (``[0-9a-fA-F\\-]{8,64}``). Opaque non-hex request identifiers are
       never promoted to ``trace_id`` — they would let any HTTP client
       poison cross-tenant log-correlation graphs (security finding A3).
    4. Freshly generated UUID4 (32-hex, no dashes).

    Other rules:

    - ``trace_id_source`` is always bound so operators can tell trusted
      parsed values apart from synthesised fallbacks.
    - ``span_id`` is bound only when ``traceparent`` parsed successfully.
    - ``request_id`` is *always* a fresh per-hop UUID4, distinct from
      ``trace_id``. An inbound ``X-Request-ID`` is never used as this hop's
      ``request_id`` — it can only act as a trace_id fallback when
      hex-shaped, and is preserved separately as ``upstream_request_id`` so
      nothing is lost regardless of shape.
    - ``amzn_trace_root`` is bound separately whenever a valid AWS header was
      present, regardless of who wins the ``trace_id`` slot.
    - ``trace_flags`` is bound whenever ``traceparent`` parsed.
    """
    context: dict[str, str] = {}

    tp_parsed = parse_traceparent(traceparent)
    amzn_parsed = parse_x_amzn_trace_id(amzn)
    upstream_req = validate_request_id(req_id)

    # Always bind a fresh per-hop request_id.
    context["request_id"] = uuid.uuid4().hex

    if upstream_req is not None:
        context["upstream_request_id"] = upstream_req

    if amzn_parsed is not None:
        context["amzn_trace_root"] = amzn_parsed["amzn_trace_root"]

    # Apply trace_id precedence ladder. Record provenance so queries can
    # distinguish trusted parsed values from synthesised fallbacks.
    if tp_parsed is not None:
        context["trace_id"] = tp_parsed["trace_id"]
        context["span_id"] = tp_parsed["span_id"]
        context["trace_flags"] = tp_parsed["trace_flags"]
        context["trace_id_source"] = TRACE_ID_SOURCE_TRACEPARENT
    elif amzn_parsed is not None:
        context["trace_id"] = amzn_parsed["amzn_trace_root"]
        context["trace_id_source"] = TRACE_ID_SOURCE_AMZN
    elif upstream_req is not None and _HEX_OR_UUID_RE.match(upstream_req) is not None:
        # Only promote the inbound request id to trace_id when it is shaped
        # like a legitimate trace identifier. Opaque strings stay preserved
        # as upstream_request_id but never contaminate trace_id.
        context["trace_id"] = upstream_req
        context["trace_id_source"] = TRACE_ID_SOURCE_REQUEST_ID
    else:
        context["trace_id"] = uuid.uuid4().hex
        context["trace_id_source"] = TRACE_ID_SOURCE_SYNTHETIC

    return context
