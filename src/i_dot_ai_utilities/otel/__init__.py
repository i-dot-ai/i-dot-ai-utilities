"""OpenTelemetry-native support for i-dot-ai services.

Public entry point:

- :func:`configure_otel`: standard OpenTelemetry SDK configuration for AWS
  Lambda and local development, without the legacy ``StructuredLogger`` bridge.

Requires the ``[otel_native]`` or ``[otel_lambda]`` optional extra. Importing
this package without ``opentelemetry-*`` installed raises ``ImportError`` at
module load so missing extras fail fast.
"""

from __future__ import annotations

from i_dot_ai_utilities.otel._configuration import configure_otel

__all__ = ["configure_otel"]
