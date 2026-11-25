"""OpenTelemetry runtime support for AWS ECS tasks on Fargate or EC2 and AWS Batch jobs.

Long-lived container processes export logs on the SDK's background
`BatchLogRecordProcessor` thread so requests never wait on network I/O. This
module adds four container safeguards around that baseline. It caps `atexit`
shutdown so a hung gateway cannot hold a stopping task past ECS `stopTimeout`,
installs a `SIGTERM` handler so plain scripts run `atexit` flushes on stop,
emits a rate-limited CloudWatch EMF failure metric, and keeps single-line JSON
stdout logs enabled by default.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
from typing import TYPE_CHECKING, Final

from opentelemetry import trace
from opentelemetry._logs import get_logger_provider
from opentelemetry.sdk.environment_variables import (
    OTEL_EXPORTER_OTLP_LOGS_TIMEOUT,
    OTEL_EXPORTER_OTLP_TIMEOUT,
    OTEL_EXPORTER_OTLP_TRACES_TIMEOUT,
)
from opentelemetry.sdk.extension.aws.resource import AwsEcsResourceDetector

from i_dot_ai_utilities.otel._attributes import iai_attribute
from i_dot_ai_utilities.otel._emf import RateLimitedFailureSignal
from i_dot_ai_utilities.otel._environment import env_number, is_container_runtime
from i_dot_ai_utilities.otel._export import (
    FailureReportingExporter,
    FailureReportingSpanExporter,
    resolve_max_connection_age_seconds,
)
from i_dot_ai_utilities.otel._runtime import Runtime, apply_aws_trace_env_defaults, unregister_sdk_atexit

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import FrameType

    from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
    from opentelemetry.sdk._logs.export import LogRecordExporter
    from opentelemetry.sdk.trace import SpanProcessor
    from opentelemetry.sdk.trace.export import SpanExporter
    from opentelemetry.util.types import AttributeValue

ENV_SHUTDOWN_TIMEOUT_MS: Final[str] = "I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS"
DEFAULT_SHUTDOWN_TIMEOUT_MS: Final[float] = 5000.0
DEFAULT_CONTAINER_LOGS_TIMEOUT_SECONDS: Final[str] = "5"
FAILURE_SIGNAL_INTERVAL_SECONDS: Final[float] = 60.0

# AWS Batch sets these variables in every job container. OpenTelemetry defines no
# semantic conventions for AWS Batch, so these attributes use the i_ai namespace.
BATCH_ENV_TO_ATTRIBUTE: Final[dict[str, str]] = {
    "AWS_BATCH_JOB_ID": iai_attribute("aws.batch.job.id"),
    "AWS_BATCH_JOB_ATTEMPT": iai_attribute("aws.batch.job.attempt"),
    "AWS_BATCH_JQ_NAME": iai_attribute("aws.batch.job.queue"),
}


def batch_resource_attributes() -> dict[str, AttributeValue]:
    """Map populated AWS Batch environment variables to `i_ai.aws.batch.*` resource attributes."""
    return {
        attribute: os.environ[env].strip()
        for env, attribute in BATCH_ENV_TO_ATTRIBUTE.items()
        if os.environ.get(env, "").strip()
    }


def _run_capped(target: Callable[[], object], timeout_seconds: float, *, name: str) -> bool:
    """Run `target` on a daemon thread and wait at most `timeout_seconds`.

    SDK `force_flush` and `shutdown` calls can block past their own timeout
    arguments on a hung connection. Running `target` on a daemon thread lets
    the caller return and the interpreter exit once `timeout_seconds` elapses.

    Returns:
        `True` if `target` finished within `timeout_seconds`, or `False` if
        the worker thread is still running.
    """
    worker = threading.Thread(target=target, name=name, daemon=True)
    worker.start()
    worker.join(timeout_seconds)
    return not worker.is_alive()


def register_capped_shutdown(
    provider: LoggerProvider,
    timeout_seconds: float,
    *,
    tracer_provider: object | None = None,
) -> None:
    """Replace the SDK's `atexit` hook with a daemon-thread shutdown capped at `timeout_seconds`.

    The SDK's default `atexit` hook waits up to 30 seconds per provider, which
    consumes or exceeds the default ECS `stopTimeout` and leaves no time for
    application cleanup. When `timeout_seconds` expires on a hung gateway, the
    interpreter exits and drops any remaining queued telemetry while stdout
    retains its log copy.
    """
    unregister_sdk_atexit(provider)
    providers: list[object] = [provider]
    if tracer_provider is not None and callable(getattr(tracer_provider, "shutdown", None)):
        unregister_sdk_atexit(tracer_provider)
        providers.append(tracer_provider)

    def _shutdown_all() -> None:
        for item in providers:
            shutdown = getattr(item, "shutdown", None)
            if callable(shutdown):
                shutdown()

    atexit.register(_run_capped, _shutdown_all, timeout_seconds, name="i-dot-ai-otel-shutdown")


def flush_otel() -> bool:
    """Flush queued OTLP logs and traces now, capped at `I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS`.

    Call this during server shutdown, such as after `yield` in a FastAPI
    lifespan, when the server exits without running `atexit` hooks. `uvicorn`
    before 0.47 and `uvicorn --workers` both skip `atexit`, so queued telemetry
    would otherwise miss the gateway on task stop.

    The flush runs on a daemon thread capped at `I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS`,
    which defaults to 5000 milliseconds, because the SDK can block past its own
    `force_flush` timeout on a hung connection and stall ECS shutdown past
    `stopTimeout`.

    Returns:
        `True` if the flush finished within the timeout, or `False` if it timed
        out or `configure_otel` has not configured a `LoggerProvider`.
    """
    log_flush = getattr(get_logger_provider(), "force_flush", None)
    if log_flush is None:
        return False
    trace_flush = getattr(trace.get_tracer_provider(), "force_flush", None)
    timeout_ms = env_number(ENV_SHUTDOWN_TIMEOUT_MS, DEFAULT_SHUTDOWN_TIMEOUT_MS)

    def _flush_all() -> None:
        log_flush(int(timeout_ms))
        if callable(trace_flush):
            trace_flush(int(timeout_ms))

    return _run_capped(_flush_all, timeout_ms / 1000, name="i-dot-ai-otel-flush")


def _exit_on_sigterm(signum: int, _frame: FrameType | None) -> None:
    """Raise `SystemExit` with `128 + signum` so main-thread `finally` blocks and `atexit` hooks run."""
    sys.exit(128 + signum)


def install_sigterm_handler() -> bool:
    """Install a `SIGTERM` handler that exits via `SystemExit` when no handler is set.

    Python's default `SIGTERM` disposition terminates the process without
    running `atexit`, dropping queued logs when ECS or AWS Batch stops a task.
    Python only allows registering signal handlers on the main thread, and this
    function preserves any custom handler already installed. `uvicorn` 0.47 and
    later restores this handler after graceful shutdown and re-raises
    `SIGTERM`, whereas older `uvicorn` installs its own handler before
    importing the app and requires calling `flush_otel` during lifespan
    shutdown.

    Returns:
        `True` if the handler was installed, or `False` if called off the main
        thread or when `SIGTERM` already has a non-default handler.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        return False
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    return True


class EcsRuntime(Runtime):
    """Configure OpenTelemetry for ECS on Fargate or EC2 and AWS Batch containers.

    Exports logs on the SDK's background thread, reports export failures via a
    rate-limited CloudWatch EMF metric, caps `atexit` shutdown, and installs a
    `SIGTERM` handler for graceful exit.
    """

    missing_endpoint_message = (
        "OTEL_EXPORTER_OTLP_ENDPOINT / OTEL_EXPORTER_OTLP_LOGS_ENDPOINT is not set; "
        "continuing with OTLP log export disabled and logs on stdout only."
    )

    def __init__(self, *, service_name: str | None) -> None:
        super().__init__(service_name=service_name)
        self.failure_signal = RateLimitedFailureSignal(
            dimension="ServiceName",
            value=service_name or "unknown",
            interval_seconds=FAILURE_SIGNAL_INTERVAL_SECONDS,
        )

    def apply_env_defaults(self, *, can_export: bool) -> None:
        super().apply_env_defaults(can_export=can_export)
        apply_aws_trace_env_defaults()
        # Leave signal-specific timeouts unset when OTEL_EXPORTER_OTLP_TIMEOUT is set,
        # because the signal-specific variables would otherwise shadow the caller's general timeout.
        if not os.environ.get(OTEL_EXPORTER_OTLP_TIMEOUT, "").strip():
            os.environ.setdefault(OTEL_EXPORTER_OTLP_LOGS_TIMEOUT, DEFAULT_CONTAINER_LOGS_TIMEOUT_SECONDS)
            os.environ.setdefault(OTEL_EXPORTER_OTLP_TRACES_TIMEOUT, DEFAULT_CONTAINER_LOGS_TIMEOUT_SECONDS)

    def resource_attributes(self) -> dict[str, AttributeValue]:
        attributes = dict(AwsEcsResourceDetector().detect().attributes)
        attributes.update(batch_resource_attributes())
        return attributes

    def log_record_processor(self, exporter: LogRecordExporter) -> LogRecordProcessor:
        return super().log_record_processor(
            FailureReportingExporter(
                exporter,
                self.failure_signal,
                max_connection_age_seconds=resolve_max_connection_age_seconds(),
            )
        )

    def span_processor(self, exporter: SpanExporter) -> SpanProcessor:
        return super().span_processor(
            FailureReportingSpanExporter(
                exporter,
                self.failure_signal,
                max_connection_age_seconds=resolve_max_connection_age_seconds(),
            )
        )

    def post_configure(self, provider: LoggerProvider) -> None:
        timeout_ms = env_number(ENV_SHUTDOWN_TIMEOUT_MS, DEFAULT_SHUTDOWN_TIMEOUT_MS)
        register_capped_shutdown(
            provider,
            timeout_ms / 1000,
            tracer_provider=trace.get_tracer_provider(),
        )
        install_sigterm_handler()
        super().post_configure(provider)


__all__ = [
    "BATCH_ENV_TO_ATTRIBUTE",
    "DEFAULT_SHUTDOWN_TIMEOUT_MS",
    "ENV_SHUTDOWN_TIMEOUT_MS",
    "EcsRuntime",
    "batch_resource_attributes",
    "flush_otel",
    "install_sigterm_handler",
    "is_container_runtime",
    "register_capped_shutdown",
]
