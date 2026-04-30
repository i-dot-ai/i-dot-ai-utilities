"""Composite OTel propagator for the Django OTel middleware.

Builds a :class:`CompositePropagator` chaining W3C Trace Context and AWS
X-Ray, arranged so W3C wins when both headers are present on the same
inbound request. Matches the existing ``middleware/_trace.py``
precedence ladder.

``X-Request-ID`` is intentionally **not** included in the propagator
chain. The existing middleware treats it as an opaque per-hop
correlation id (never as a W3C trace), and the OTel alternative
preserves that contract — ``request_id`` continues to be bound onto the
log context by the middleware itself, not by span context.

Pure wiring module: no module-level side effects, no global propagator
mutation. ``set_global_textmap`` is called exclusively from
``setup.configure_otel_for_django``.
"""

from __future__ import annotations

from opentelemetry.propagators.aws import AwsXRayPropagator
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator


def build_composite_propagator() -> CompositePropagator:
    """Return a composite propagator giving W3C Trace Context precedence.

    Ordering is significant. :class:`CompositePropagator` runs every
    propagator on ``extract`` and the **last** propagator to write a
    context key wins. To match the existing middleware's precedence
    (W3C beats X-Ray when both headers are present), X-Ray runs first
    and W3C runs last.

    Both propagators still contribute on ``inject``, so outbound
    requests carry both ``traceparent`` and ``X-Amzn-Trace-Id``
    regardless of order.
    """
    return CompositePropagator(
        [
            AwsXRayPropagator(),
            TraceContextTextMapPropagator(),
        ]
    )


__all__ = ["build_composite_propagator"]
