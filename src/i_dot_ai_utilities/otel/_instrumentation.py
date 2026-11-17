"""Auto-instrument every installed OpenTelemetry instrumentation package.

Uses the same ``opentelemetry_instrumentor`` entry points as the
``opentelemetry-instrument`` CLI, so installing a framework extra such as
``otel_native_fastapi`` is enough to instrument that framework. The CLI's own
``initialize()`` is not used because it also runs the distro configurator,
which would replace the providers ``configure_otel`` sets up.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import entry_points

from opentelemetry.instrumentation.dependencies import get_dist_dependency_conflicts
from opentelemetry.instrumentation.environment_variables import OTEL_PYTHON_DISABLED_INSTRUMENTATIONS


def disabled_instrumentations() -> set[str]:
    """Return entry point names listed in ``OTEL_PYTHON_DISABLED_INSTRUMENTATIONS``."""
    raw = os.environ.get(OTEL_PYTHON_DISABLED_INSTRUMENTATIONS, "")
    return {name.strip() for name in raw.split(",") if name.strip()}


def instrument_installed_libraries(*, skip: frozenset[str] = frozenset()) -> list[str]:
    """Instrument each installed library whose instrumentor's version requirements are met.

    ``skip`` holds entry point names a runtime instruments itself (for
    example ``aws-lambda``, which must wrap the handler in a fixed order).
    Returns the names that were instrumented. A failing instrumentor is
    reported on stderr and skipped, so one broken package never stops start-up.
    """
    excluded = disabled_instrumentations() | skip
    instrumented: list[str] = []
    for entry_point in entry_points(group="opentelemetry_instrumentor"):
        if "*" in excluded or entry_point.name in excluded:
            continue
        try:
            if entry_point.dist is not None and get_dist_dependency_conflicts(entry_point.dist) is not None:
                # The instrumented library is missing or the wrong version; nothing to instrument.
                continue
            instrumentor = entry_point.load()()
            if not instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.instrument(skip_dep_check=True)
            instrumented.append(entry_point.name)
        except ModuleNotFoundError:
            # The instrumented library is not installed (botocore's package also ships aiobotocore's instrumentor).
            continue
        except Exception as exc:  # noqa: BLE001 - instrumentation must never break start-up
            sys.stderr.write(
                f"i_dot_ai_utilities.otel: failed to instrument {entry_point.name!r} "
                f"({type(exc).__name__}); skipping.\n"
            )
    return instrumented


def uninstrument_installed_libraries() -> None:
    """Undo :func:`instrument_installed_libraries` (used by tests)."""
    for entry_point in entry_points(group="opentelemetry_instrumentor"):
        try:
            instrumentor = entry_point.load()()
            if instrumentor.is_instrumented_by_opentelemetry:
                instrumentor.uninstrument()
        except Exception:  # noqa: BLE001, S112 - best-effort teardown
            continue


__all__ = ["disabled_instrumentations", "instrument_installed_libraries", "uninstrument_installed_libraries"]
