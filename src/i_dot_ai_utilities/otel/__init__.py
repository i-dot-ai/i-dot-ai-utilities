"""OpenTelemetry for i.AI Python services on AWS Lambda, ECS (Fargate or EC2), AWS Batch, and locally.

Public API:

- :func:`configure_otel`: configure the OpenTelemetry SDK, the Python
  ``logging`` handler, and auto-instrumentation for the detected runtime.
- :func:`iai_attribute`: build a custom attribute name under the ``i_ai`` namespace.
- :func:`validate_attributes`: check attribute names follow the naming rules.
- :data:`I_AI_NAMESPACE`: the custom attribute namespace, ``"i_ai"``.

Requires the ``[otel_native]`` extra, plus ``[otel_native_lambda]`` on Lambda
or a framework extra such as ``[otel_native_fastapi]``. Importing this package
without ``opentelemetry-*`` installed raises ``ImportError`` so missing
extras fail fast. See ``README.md`` in this package for usage.
"""

from __future__ import annotations

from i_dot_ai_utilities.otel._attributes import I_AI_NAMESPACE, iai_attribute, validate_attributes
from i_dot_ai_utilities.otel._configuration import configure_otel

__all__ = ["I_AI_NAMESPACE", "configure_otel", "iai_attribute", "validate_attributes"]
