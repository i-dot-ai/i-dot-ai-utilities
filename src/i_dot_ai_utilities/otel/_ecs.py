"""Long-running AWS containers: ECS on Fargate or EC2, and AWS Batch jobs.

The process keeps running between requests, so logs are exported by the SDK's
background ``BatchLogRecordProcessor`` thread and the request path never waits
on the network. This runtime adds what the SDK lacks for containers: a capped
shutdown so a hung gateway cannot hold a stopping task past ECS
``stopTimeout``, a SIGTERM handler so plain scripts still flush on stop, a
rate-limited EMF failure metric, and JSON stdout logs kept by default.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
from typing import TYPE_CHECKING, Final

from opentelemetry.sdk.environment_variables import (
    OTEL_EXPORTER_OTLP_LOGS_TIMEOUT,
    OTEL_EXPORTER_OTLP_TIMEOUT,
)
from opentelemetry.sdk.extension.aws.resource import AwsEcsResourceDetector

from i_dot_ai_utilities.otel._attributes import iai_attribute
from i_dot_ai_utilities.otel._emf import RateLimitedFailureSignal
from i_dot_ai_utilities.otel._environment import env_number, keep_stdout_logs, logs_export_disabled
from i_dot_ai_utilities.otel._export import FailureReportingExporter
from i_dot_ai_utilities.otel._runtime import Runtime, apply_aws_trace_env_defaults, unregister_sdk_atexit
from i_dot_ai_utilities.otel._stdout import install_stdout_handler

if TYPE_CHECKING:
    from types import FrameType

    from opentelemetry.sdk._logs import LoggerProvider, LogRecordProcessor
    from opentelemetry.sdk._logs.export import LogRecordExporter
    from opentelemetry.util.types import AttributeValue

ENV_SHUTDOWN_TIMEOUT_MS: Final[str] = "I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS"
DEFAULT_SHUTDOWN_TIMEOUT_MS: Final[float] = 5000.0
DEFAULT_CONTAINER_LOGS_TIMEOUT_SECONDS: Final[str] = "5"
FAILURE_SIGNAL_INTERVAL_SECONDS: Final[float] = 60.0

# AWS Batch sets these in every job container. There are no semantic convention
# names for Batch, so they go under the i_ai namespace.
BATCH_ENV_TO_ATTRIBUTE: Final[dict[str, str]] = {
    "AWS_BATCH_JOB_ID": iai_attribute("aws.batch.job.id"),
    "AWS_BATCH_JOB_ATTEMPT": iai_attribute("aws.batch.job.attempt"),
    "AWS_BATCH_JQ_NAME": iai_attribute("aws.batch.job.queue"),
}


def is_container_runtime() -> bool:
    """Return True on ECS (Fargate or EC2) or AWS Batch."""
    return bool(
        os.environ.get("ECS_CONTAINER_METADATA_URI_V4", "").strip() or os.environ.get("AWS_BATCH_JOB_ID", "").strip()
    )


def batch_resource_attributes() -> dict[str, AttributeValue]:
    """Return AWS Batch job attributes for the variables that are set."""
    return {
        attribute: os.environ[env].strip()
        for env, attribute in BATCH_ENV_TO_ATTRIBUTE.items()
        if os.environ.get(env, "").strip()
    }


def register_capped_shutdown(provider: LoggerProvider, timeout_seconds: float) -> None:
    """Replace the SDK's unbounded ``atexit`` shutdown with one that waits at most ``timeout_seconds``.

    The SDK waits up to 30 s for its export thread, longer than the default
    ECS ``stopTimeout``. Shutdown runs on a daemon thread, so if it is still
    blocked when the cap expires the interpreter exits without it and the
    remaining queued logs are dropped (they are still on stdout).
    """
    unregister_sdk_atexit(provider)

    def _shutdown() -> None:
        worker = threading.Thread(target=provider.shutdown, name="i-dot-ai-otel-shutdown", daemon=True)
        worker.start()
        worker.join(timeout_seconds)

    atexit.register(_shutdown)


def _exit_on_sigterm(signum: int, _frame: FrameType | None) -> None:
    # Raising SystemExit unwinds the main thread, so ``finally`` blocks and ``atexit`` (our flush) run.
    sys.exit(128 + signum)


def install_sigterm_handler() -> bool:
    """Exit cleanly on SIGTERM when nothing else handles it. Returns True when installed.

    A handler that is already set is left in place. Without one, Python dies on
    SIGTERM without running ``atexit``, so queued logs would be lost when ECS or
    Batch stops the task. uvicorn needs this handler too: after its own graceful
    shutdown it restores the previous handler and re-raises the signal.
    """
    if threading.current_thread() is not threading.main_thread():
        return False
    if signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL:
        return False
    signal.signal(signal.SIGTERM, _exit_on_sigterm)
    return True


class EcsRuntime(Runtime):
    """ECS on Fargate or EC2, and AWS Batch: background export, capped shutdown, stdout kept."""

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

    def apply_env_defaults(self, *, endpoint_configured: bool) -> None:
        super().apply_env_defaults(endpoint_configured=endpoint_configured)
        apply_aws_trace_env_defaults()
        if not os.environ.get(OTEL_EXPORTER_OTLP_TIMEOUT, "").strip():
            os.environ.setdefault(OTEL_EXPORTER_OTLP_LOGS_TIMEOUT, DEFAULT_CONTAINER_LOGS_TIMEOUT_SECONDS)

    def resource_attributes(self) -> dict[str, AttributeValue]:
        attributes = dict(AwsEcsResourceDetector().detect().attributes)
        attributes.update(batch_resource_attributes())
        return attributes

    def log_record_processor(self, exporter: LogRecordExporter) -> LogRecordProcessor:
        return super().log_record_processor(FailureReportingExporter(exporter, self.failure_signal))

    def post_configure(self, provider: LoggerProvider) -> None:
        timeout_ms = env_number(ENV_SHUTDOWN_TIMEOUT_MS, DEFAULT_SHUTDOWN_TIMEOUT_MS)
        register_capped_shutdown(provider, timeout_ms / 1000)
        install_sigterm_handler()
        # Export off must never mean logging off, so stdout is forced on in that case.
        if logs_export_disabled() or keep_stdout_logs(default=True):
            install_stdout_handler()


__all__ = [
    "BATCH_ENV_TO_ATTRIBUTE",
    "DEFAULT_SHUTDOWN_TIMEOUT_MS",
    "ENV_SHUTDOWN_TIMEOUT_MS",
    "EcsRuntime",
    "batch_resource_attributes",
    "install_sigterm_handler",
    "is_container_runtime",
    "register_capped_shutdown",
]
