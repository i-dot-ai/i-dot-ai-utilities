"""Sample Lambda handler module for no-layer OTel instrumentation spike and tests."""

from __future__ import annotations

import logging
import os
from typing import Any

from opentelemetry import trace

from i_dot_ai_utilities.otel import configure_otel

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def _raise_synthetic_failure() -> None:
    msg = "synthetic handler failure"
    raise ValueError(msg)


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    mode = event.get("mode", "ok")
    span = trace.get_current_span()
    span_ctx = span.get_span_context()

    # Emit an internal opentelemetry.* log to prove the filter blocks it from OTLP.
    logging.getLogger("opentelemetry.sdk.internal_test").warning("internal-otel-noise")

    logger.info(
        "Authorisation request started for %s",
        event.get("application", "demo-app"),
        extra={"application": event.get("application", "demo-app"), "mode": mode},
    )

    if mode == "caught_error":
        logger.warning("Input validation failed", extra={"error.type": "400"})
        return {
            "statusCode": 400,
            "trace_id": format(span_ctx.trace_id, "032x"),
            "span_id": format(span_ctx.span_id, "016x"),
        }

    if mode == "raise":
        try:
            _raise_synthetic_failure()
        except ValueError:
            logger.exception("Authorisation request failed")
            raise

    if mode == "batch_burst":
        count = int(event.get("count", 5))
        for idx in range(count):
            logger.info("Batch burst line %d", idx, extra={"seq": idx})

    return {
        "statusCode": 200,
        "trace_id": format(span_ctx.trace_id, "032x"),
        "span_id": format(span_ctx.span_id, "016x"),
    }


_explicit_service_name = os.environ.get("TEST_EXPLICIT_SERVICE_NAME") or None
configure_otel(service_name=_explicit_service_name)

if os.environ.get("TEST_CALL_CONFIGURE_TWICE") == "1":
    configure_otel(service_name=_explicit_service_name)
