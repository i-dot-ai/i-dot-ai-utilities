"""Discover and enable installed OpenTelemetry library instrumentors via entry points.

`configure_otel` loads `opentelemetry_instrumentor` entry points itself rather
than calling `opentelemetry-instrument`'s `initialize()` helper. Calling
`initialize()` would run the distro configurator a second time and replace the
SDK providers that `configure_otel` already configured.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import entry_points

from opentelemetry.instrumentation.dependencies import get_dist_dependency_conflicts
from opentelemetry.instrumentation.environment_variables import OTEL_PYTHON_DISABLED_INSTRUMENTATIONS


def disabled_instrumentations() -> set[str]:
    """Parse comma-separated names from `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS`, where `"*"` disables all."""
    raw = os.environ.get(OTEL_PYTHON_DISABLED_INSTRUMENTATIONS, "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def instrument_installed_libraries(*, skip: frozenset[str] = frozenset()) -> list[str]:
    """Activate every installed `opentelemetry_instrumentor` entry point whose target library is present.

    Logs unexpected instrumentor failures to `stderr` and continues so a
    broken third-party instrumentor never prevents application startup.

    Args:
        skip: Entry point names that the active runtime instruments directly,
            such as `"aws-lambda"` when wrapping the handler in a fixed order.

    Returns:
        Entry point names that were instrumented or already active.
    """
    excluded = disabled_instrumentations() | skip
    instrumented: list[str] = []
    for entry_point in entry_points(group="opentelemetry_instrumentor"):
        if "*" in excluded or entry_point.name in excluded:
            continue
        try:
            if entry_point.dist is not None and get_dist_dependency_conflicts(entry_point.dist) is not None:
                # Skip instrumentors whose target package is missing or outside its supported version range.
                continue
            instrumentor = entry_point.load()()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument(skip_dep_check=True)
            instrumented.append(entry_point.name)
        except ModuleNotFoundError:
            # Multi-hook packages such as opentelemetry-instrumentation-botocore also register
            # entry points for optional libraries like aiobotocore that may not be installed.
            continue
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"i_dot_ai_utilities.otel: failed to instrument {entry_point.name!r} "
                f"({type(exc).__name__}); skipping.\n"
            )
    return instrumented


def uninstrument_installed_libraries() -> None:
    """Deactivate every active OpenTelemetry instrumentor during test teardown."""
    for entry_point in entry_points(group="opentelemetry_instrumentor"):
        try:
            instrumentor = entry_point.load()()
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
        except Exception:  # noqa: BLE001, S112
            continue


__all__ = ["disabled_instrumentations", "instrument_installed_libraries", "uninstrument_installed_libraries"]
