"""OpenTelemetry for i.AI Python services on AWS Lambda, ECS on Fargate or EC2, AWS Batch, and local runs.

Install the `otel_native` extra for every runtime, plus `otel_native_lambda` on
Lambda or a framework extra such as `otel_native_fastapi`. Importing this
package without `opentelemetry-*` installed raises `ImportError` at import time
so missing extras fail before the service starts.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from i_dot_ai_utilities.otel._attributes import I_AI_NAMESPACE, iai_attribute, validate_attributes

if TYPE_CHECKING:
    from i_dot_ai_utilities.otel._configuration import configure_otel
    from i_dot_ai_utilities.otel._ecs import flush_otel

__all__ = ["I_AI_NAMESPACE", "configure_otel", "flush_otel", "iai_attribute", "validate_attributes"]


def __getattr__(name: str) -> object:
    if name == "configure_otel":
        from i_dot_ai_utilities.otel._configuration import configure_otel  # noqa: PLC0415

        return configure_otel
    if name == "flush_otel":
        from i_dot_ai_utilities.otel._ecs import flush_otel  # noqa: PLC0415

        return flush_otel
    msg = f"module {__name__!r} has no attribute {name!r}"
    raise AttributeError(msg)
