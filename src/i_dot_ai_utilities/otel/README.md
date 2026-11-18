# OpenTelemetry (`i_dot_ai_utilities.otel`)

`i_dot_ai_utilities.otel` sends Python application logs to the i.AI `otel-gateway` using the standard OpenTelemetry SDK. Call `configure_otel()` once at start-up. It detects the runtime, configures the SDK, attaches an OpenTelemetry handler to Python's root logger, and auto-instruments every installed instrumentation package. Application code then uses plain `logging`.

It is separate from `StructuredLogger` and the legacy `i_dot_ai_utilities.logging.otel` bridge, and imports neither. Some other modules in this package still take a `StructuredLogger`; see [Modules that still use `StructuredLogger`](#modules-that-still-use-structuredlogger).

## Install

Every runtime needs the `otel_native` base extra. Add one extra per framework or platform you use:

| Extra | Adds | Use for |
|---|---|---|
| `otel_native` | SDK, OTLP/HTTP exporter, AWS resource detectors, X-Ray propagator | ECS, AWS Batch and local runs with no framework |
| `otel_native_lambda` | AWS Lambda and botocore instrumentation | Python Lambdas |
| `otel_native_fastapi` | FastAPI instrumentation | FastAPI services |

```bash
pip install --pre "i-dot-ai-utilities[otel_native_fastapi]"
```

The extras pin the OpenTelemetry SDK to one minor version (currently 1.45), because this package relies on a few SDK internals. Tests in `__tests__/test_unit_runtime.py` (`TestSdkInternals`) fail if an upgrade removes one.

## Runtimes

`configure_otel()` picks a runtime from the environment. Pass `runtime="lambda" | "ecs" | "local"` to override it.

| Runtime | Detected by | How logs are exported |
|---|---|---|
| `lambda` | `AWS_LAMBDA_FUNCTION_NAME` | Synchronously at the end of each invocation, bounded by the export timeout, with an outage circuit breaker |
| `ecs` | `ECS_CONTAINER_METADATA_URI_V4` or `AWS_BATCH_JOB_ID` | On the SDK's background thread; requests never wait on the network |
| `local` | Neither of the above | Background thread to OTLP if an endpoint is set, otherwise stdout only |

The `ecs` runtime covers ECS on Fargate, ECS on EC2 (including GPU instances) and AWS Batch jobs. All three expose the ECS task metadata endpoint, which `AwsEcsResourceDetector` reads for `aws.ecs.*` resource attributes. Batch jobs also get `i_ai.aws.batch.job.id`, `i_ai.aws.batch.job.attempt` and `i_ai.aws.batch.job.queue`.

Assumption: AWS Batch sets `ECS_CONTAINER_METADATA_URI_V4` in job containers on both EC2 and Fargate compute environments. Detection also checks `AWS_BATCH_JOB_ID`, so the runtime is still `ecs` if it does not; only the `aws.ecs.*` attributes would be missing.

## FastAPI on ECS

Call `configure_otel()` before importing FastAPI. The FastAPI instrumentor works by replacing `fastapi.FastAPI`, so an app created before the call is not instrumented.

```python
# main.py
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from i_dot_ai_utilities.otel import configure_otel, flush_otel, iai_attribute

configure_otel()

from fastapi import FastAPI  # noqa: E402

logger = logging.getLogger(__name__)
CASE_ID = iai_attribute("casework.case.id")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    yield
    flush_otel()


app = FastAPI(lifespan=lifespan)


@app.get("/cases/{case_id}")
def read_case(case_id: int) -> dict[str, int]:
    logger.info("Case requested", extra={CASE_ID: case_id})
    return {"case_id": case_id}
```

Every log line inside a request carries that request's `trace_id` and `span_id`.

uvicorn's own server and access logs are not exported over OTLP. uvicorn turns off propagation on its `uvicorn` and `uvicorn.access` loggers, so they never reach the root handler and stay on uvicorn's console output only.

Under gunicorn, keep `configure_otel()` at module level in the app module. With the default (no `--preload`), each worker imports the app and configures itself. With `--preload`, the SDK restarts its export thread in each worker after the fork. A uvicorn worker without `--preload` flushes its queued logs when gunicorn stops it (checked with gunicorn 27.0 and uvicorn 0.54). Unknown: whether sync workers and `--preload` workers do the same. Their logs are on stdout either way.

### Flush logs on shutdown

Call `flush_otel()` after `yield` in the lifespan, as in the example above. It exports the queued logs and waits at most `I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS`, so a hung gateway cannot hold the task past `stopTimeout`. The lifespan shutdown runs after the server has finished its open requests, so the call does not delay a response.

Some servers end the process without running Python's `atexit` hooks, so the library's own exit flush never runs. Without `flush_otel()`, the logs queued since the last export (up to 1 second by default) miss the gateway when the task stops. They are still on stdout.

| Server | Exit flush runs without `flush_otel()` |
|---|---|
| `uvicorn main:app`, uvicorn before 0.47 | No |
| `uvicorn main:app`, uvicorn 0.47 or later | Yes |
| `uvicorn main:app --workers N` | No |
| gunicorn with `uvicorn.workers.UvicornWorker` | Yes |

Checked with uvicorn 0.31.1, 0.46.0, 0.47.0 and 0.54.0, and gunicorn 27.0 without `--preload`. The `--workers` row was checked on uvicorn 0.54.0 only. Unknown: other servers, such as hypercorn and granian. Calling `flush_otel()` is safe on all of them.

### Start the server as the container's main process

ECS and Batch send SIGTERM only to the container's main process (PID 1). If that process is a shell script that starts the server in the background, the server never receives the signal. It keeps running until the task is killed at `stopTimeout`, and nothing is flushed. No library code can fix that, so fix it in the container.

End an entrypoint script with `exec`, so the server replaces the shell and becomes PID 1:

```bash
#!/bin/bash
set -e
alembic upgrade head
exec uvicorn backend.main:app --host 0.0.0.0 --port 8080
```

Do not start the server with `uvicorn ... &` followed by `wait`. In a Dockerfile, use the JSON form, `CMD ["uvicorn", "backend.main:app"]`, because the shell form runs the command under `/bin/sh -c`. A `uv run` prefix is fine: `uv run` passes the signal on to the server (checked with uv 0.12.3).

## Django

There is no `otel_native_django` extra yet. `configure_otel()` still enables the Django instrumentation whenever `opentelemetry-instrumentation-django` is installed, which the `all` and legacy `otel_django` extras do.

Set `DJANGO_SETTINGS_MODULE` before calling `configure_otel()`. If it is unset, the instrumentor configures Django with empty settings, and the project's own settings are never loaded. To turn the instrumentation off, set `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=django`.

## AWS Batch jobs and plain scripts

```python
import logging

from i_dot_ai_utilities.otel import configure_otel

configure_otel()
logger = logging.getLogger(__name__)


def main() -> None:
    logger.info("Ingest started")


if __name__ == "__main__":
    main()
```

When ECS or Batch stops a task it sends SIGTERM. Python's default reaction kills the process without running `atexit`, so queued logs would be lost. On the `ecs` runtime, `configure_otel()` installs a SIGTERM handler that exits with code `143` (`128 + SIGTERM`) through `sys.exit`, so `finally` blocks and the log flush run. It only does this when no SIGTERM handler is already set.

A web server can set its own SIGTERM handler first, or exit without running `atexit`. For a web service, call `flush_otel()` as described in [Flush logs on shutdown](#flush-logs-on-shutdown).

Python waits for non-daemon threads to finish before it runs the flush. A background thread that never stops holds the process until ECS kills it, and the queued logs are lost from OTLP. Create background threads with `daemon=True`, or stop them in a `finally` block.

## Lambda

Define the handler first, then call `configure_otel()` at the bottom of the handler module, so the Lambda instrumentor can wrap the function named in `_HANDLER` (or `ORIG_HANDLER`) without a Lambda layer:

```python
import logging
from typing import Any

from aws_lambda_powertools.utilities.typing import LambdaContext

from i_dot_ai_utilities.otel import configure_otel, iai_attribute

logger = logging.getLogger(__name__)
APPLICATION = iai_attribute("auth.application")


def lambda_handler(event: dict[str, Any], context: LambdaContext) -> dict[str, int]:
    logger.info(
        "Authorisation request started",
        extra={APPLICATION: event.get("application", "unknown")},
    )
    return {"statusCode": 200}


# Call after defining lambda_handler so the instrumentor wraps _HANDLER.
configure_otel()
```

If `configure_otel()` runs before the handler is defined, it cannot wrap the handler, so nothing flushes at the end of each invocation. It writes `call configure_otel() after defining the handler` to stderr and keeps the runtime's stdout logging, whatever `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS` says.

## Naming attributes

Attribute names are shared across every service that sends to `otel-gateway`. Pick them using these steps, in order:

1. Use an OpenTelemetry semantic convention attribute if one fits. Look it up in the [attribute registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/); logs, traces, metrics and resources all share it. For example, use `user.email` for an email address, `url.full` for a URL and `error.type` for an error class.
2. Otherwise, put the name under the `i_ai` namespace as `i_ai.<service>.<object>.<property>`. For example, `i_ai.auth.decision.reason`.
3. Never add your own names under an OpenTelemetry namespace such as `user.`, `http.`, `app.` or `service.`, even if `user.my_field` reads well. OpenTelemetry can define that name later with a different meaning.
4. Write each level in lowercase `snake_case`, with dots between levels. For example, `i_ai.auth.token.key_id`, not `i_ai.auth.tokenKeyId`.

The registry shows a stability for each attribute. `Stable` names will not change. `Development` names, such as `user.email`, may be renamed in a later release of the conventions. The full rules are in the [naming specification](https://opentelemetry.io/docs/specs/semconv/general/naming/).

### Helpers

```python
from i_dot_ai_utilities.otel import I_AI_NAMESPACE, iai_attribute, validate_attributes

iai_attribute("auth.decision.reason")  # "i_ai.auth.decision.reason"
iai_attribute("auth.tokenKeyId")       # raises ValueError

validate_attributes({"user.email": email, iai_attribute("auth.token.key_id"): key_id})  # passes
validate_attributes({"application": "casework"})  # raises ValueError listing "application"
```

Build each name once, as a module-level constant, as the examples above do. A bad name then fails when the module is imported, which a test run catches. Called inline, as in `extra={iai_attribute("..."): value}`, a bad name raises from the logging call instead: the log line is lost and the exception lands in the request.

`iai_attribute()` requires at least `<service>.<property>`. `validate_attributes()` accepts any name in the semantic convention registry that ships with the installed SDK, including template names such as `http.request.header.<key>` with any key (`http.request.header.content-type`), plus any well-formed `i_ai.` name.

### Enforcement

Every log record is checked, whether or not the helpers were used. A non-compliant key is renamed to `i_ai.unnamespaced.<key>` before export, and a warning naming the key is written to stderr once per process. No data is lost, and offending keys are easy to find in the backend. The stdout copy of a log keeps the original key.

Set `I_DOT_AI_OTEL_STRICT_ATTRIBUTES=true` in tests or CI to raise `ValueError` from the logging call instead. It works with export off (`OTEL_LOGS_EXPORTER=none`), which is how most test suites run.

On Lambda, the runtime adds `aws_request_id` to every record. It is exported as the semantic convention `faas.invocation_id`.

Standard attributes the SDK adds itself, such as `code.file.path`, `code.function.name`, `code.line.number` and `exception.*`, are semantic conventions and pass unchanged.

Never log bearer tokens, `Authorization` headers, JWT segments or raw request bodies. The library does not copy the Lambda event or HTTP request onto log records.

### Names the gateway masks

The gateway replaces the value of any attribute whose key matches one of its redaction patterns, so avoid these words in names that hold non-secret data:

- keys containing `password`, `passwd`, `secret`, `authorization`, `cookie`, `api_key` or `access_key`. For example, the value of `i_ai.storage.access_key_id` arrives masked.
- keys ending in `token`, such as `i_ai.auth.session_token`. Keys such as `gen_ai.usage.input_tokens` are not affected.

It also masks values that look like an AWS access key ID, a JWT, a bearer token or an `sk-` API key, whatever the key is called. The patterns are in `collector/config.yaml.tftpl` in the `core-logging` repository.

## Service name

`service.name` is resolved in this order:

1. `OTEL_SERVICE_NAME`, or `service.name` in `OTEL_RESOURCE_ATTRIBUTES`. The shared Terraform modules set `OTEL_SERVICE_NAME` when `otel_gateway.enabled = true`.
2. `service_name="..."` passed to `configure_otel()`. Use it only where nothing sets the environment variable, such as a local run.
3. Neither: if an OTLP endpoint is set, `configure_otel()` warns once on stderr, defaults `OTEL_LOGS_EXPORTER` to `none` and writes logs to stdout only.

The environment wins because the gateway's `filter/service_allowlist` processor silently drops logs whose `service.name` is not on `allowed_service_names`, while still returning HTTP `200`. The name Terraform sets is the one on the allow-list. For the same reason nothing is exported without a name: the SDK would send `unknown_service`, which the gateway drops.

## Deployment environment

`deployment.environment.name` defaults to the `ENVIRONMENT` environment variable, which i.AI services already set. Loki indexes it as a label. Set `deployment.environment.name` in `OTEL_RESOURCE_ATTRIBUTES` to override it.

## Environment variables

`configure_otel()` sets defaults with `os.environ.setdefault`, so a variable you set always wins.

### All runtimes

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_SERVICE_NAME` | _(from Terraform)_ | Service name; wins over the `service_name` argument |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(from Terraform SSM)_ | Base OTLP/HTTP URL, for example `https://otel-gateway.dev.i.ai.gov.uk:4318`; the SDK appends `/v1/logs` |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | _(unset)_ | Logs-specific override, used verbatim |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | OTLP transport. `http/protobuf` is the only supported value |
| `OTEL_EXPORTER_OTLP_HEADERS` | _(unset)_ | Comma-separated `key=value` headers |
| `OTEL_RESOURCE_ATTRIBUTES` | _(unset)_ | Extra resource attributes, for example `service.namespace=casework` |
| `ENVIRONMENT` | _(set by the service)_ | Default for `deployment.environment.name` |
| `OTEL_LOGS_EXPORTER` | `otlp` when an endpoint and a service name are set; otherwise `none` | Logs exporter |
| `OTEL_TRACES_EXPORTER` | `none` | Off because `otel-gateway` only has a logs pipeline |
| `OTEL_METRICS_EXPORTER` | `none` | Off because `otel-gateway` only has a logs pipeline |
| `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` | _(unset)_ | Comma-separated instrumentations to skip, for example `fastapi`, or `*` for all |
| `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS` | `true` | Also write application logs to stdout |
| `I_DOT_AI_OTEL_STRICT_ATTRIBUTES` | `false` | Raise on non-compliant attribute names instead of renaming them |

`I_DOT_AI_OTEL_LAMBDA_KEEP_STDOUT_LOGS` is the earlier name for `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS` and is still accepted. The new name wins if both are set.

### Protocol

OTLP over HTTP (`http/protobuf`) is the only supported protocol. The extras install only the HTTP exporter, and every outage, shutdown and SIGTERM test runs over HTTP. The SDK rejects `http/json`, and rejects `grpc` because the gRPC exporter is not installed. When it does, `configure_otel()` warns once on stderr, turns export off and writes logs to stdout only, so a wrong setting cannot stop the application starting.

On `lambda` and `ecs`, `OTEL_PYTHON_ID_GENERATOR` defaults to `xray` and `OTEL_PROPAGATORS` to `xray,tracecontext,baggage`, so trace IDs are X-Ray compatible.

### `ecs` runtime

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_EXPORTER_OTLP_LOGS_TIMEOUT` | `5` (not set if `OTEL_EXPORTER_OTLP_TIMEOUT` is set) | Per-batch export timeout in seconds |
| `I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS` | `5000` | Longest time that shutdown, and each `flush_otel()` call, waits for queued logs to export |
| `OTEL_BLRP_SCHEDULE_DELAY` | `1000` (SDK default) | Background export interval in milliseconds |
| `OTEL_BLRP_MAX_QUEUE_SIZE` | `2048` (SDK default) | Records buffered before the oldest are dropped |

### `lambda` runtime

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_EXPORTER_OTLP_LOGS_TIMEOUT` | `2` (not set if `OTEL_EXPORTER_OTLP_TIMEOUT` is set) | Per-batch export timeout in seconds (the Python SDK reads this in seconds, so `2` is 2 seconds, not 2 milliseconds) |
| `OTEL_BLRP_MAX_EXPORT_BATCH_SIZE` | `512` | Records per OTLP POST, so a normal invocation exports in one batch |
| `OTEL_BLRP_MAX_QUEUE_SIZE` | `2048` | Records buffered across invocations |
| `OTEL_BLRP_SCHEDULE_DELAY` | `60000` | Background export interval; flushing happens at invocation boundaries instead |
| `I_DOT_AI_OTEL_LAMBDA_FLUSH_MARGIN_MS` | `250` | Remaining-time buffer required on top of the export timeout before flushing |
| `I_DOT_AI_OTEL_LAMBDA_CIRCUIT_BREAKER_SECONDS` | `30` | Seconds to skip synchronous flushes after a failure |

## Stdout logging

Application logs go to stdout as well as OTLP by default on every runtime, so CloudWatch keeps a copy during a gateway outage. A successful response from the gateway means it queued the batch, not that it stored it, so the gateway's own guidance is to keep a copy. The cost is CloudWatch ingestion for every log line.

On `ecs` and `local`, each log is one JSON object per line. Each line has `timestamp`, `level`, `logger`, `message`, `trace_id` and `span_id` inside a span, every `extra=` attribute, and `exception` when there is one. A single line per record means `awslogs` never splits a multi-line traceback into separate events.

The stdout handler is only added when the root logger has no other handlers. If the application already configures console logging, that is left as is. Set `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS=false` to send logs to OTLP only. When no OTLP endpoint is configured, or `OTEL_LOGS_EXPORTER=none`, stdout stays on regardless, so turning export off never turns logging off.

On Lambda, the runtime's own `awslambdaric` stdout handlers already write to CloudWatch, so `configure_otel()` leaves them in place and adds none. With `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS=false` it removes them, but only once the handler is wrapped and only while export is on.

## Modules that still use `StructuredLogger`

`auth`, `metrics`, `file_store` and `litellm` take a `StructuredLogger` in their constructors. `StructuredLogger` prints straight to stdout, so their logs reach CloudWatch but not the gateway. Moving them to plain `logging` is separate work.

Passing a `logging.Logger` instead works only for `metrics`, which calls nothing but `.exception()`. The others pass keyword arguments, as in `logger.info("... {key}", key=key)`, which `logging` rejects with `TypeError`. `auth` only does so in `debug` calls, so it works until the logger's level is `DEBUG`.

## Log levels

`configure_otel()` lowers the root logger from Python's default `WARNING` to `INFO`, so `logger.info(...)` works without extra set-up. It cannot tell the default from a deliberate `WARNING`. To keep `WARNING`, set it after calling `configure_otel()`.

The OpenTelemetry handler stays on the root logger when the application later calls `logging.config.dictConfig`, `fileConfig` or `basicConfig`. Those calls can still change the root level. Alembic's default `alembic.ini` sets it to `WARN`, so `INFO` logs stop after a migration runs in the same process unless you change that file.

It sets the `botocore`, `boto3` and `urllib3` loggers to `WARNING` so their INFO messages are not exported, unless your application set their level first.

Logs from `opentelemetry.*` loggers are never exported, so exporter errors cannot loop back into the exporter.

## Failure behaviour

A gateway that is down, slow, hung or rejecting requests must never break or slow the application.

### Failure signal

When an export fails, the library writes one JSON line to stdout that is also a CloudWatch Embedded Metric Format (EMF) metric: `level="WARNING"`, `event="otel_log_export_failed"`, `ExportFailures = 1` in namespace `i-dot-ai/otel`. The payload contains no endpoints, headers, bodies or exception text. CloudWatch Logs extracts the metric from Lambda logs and from ECS `awslogs` alike.

| Runtime | Dimension | Rate |
|---|---|---|
| `lambda` | `FunctionName` | Once per failed or skipped flush, so the alarm keeps firing for the whole outage |
| `ecs` | `ServiceName` | At most once per 60 seconds per process |

On Lambda, enable `export_failures_threshold = 0` on `modules/observability/lambda-alarms` to alert on it.

### Containers (`ecs`)

Requests only add log records to a bounded in-memory queue; the SDK's background thread does the network I/O. During an outage the queue fills to `OTEL_BLRP_MAX_QUEUE_SIZE` and the oldest records are dropped from OTLP; they are still on stdout.

The SDK's own shutdown waits up to 30 seconds for the export thread. That is the whole default ECS `stopTimeout` (30 seconds), leaving no time for the application's own shutdown. `configure_otel()` replaces it with a shutdown capped at `I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS`. `flush_otel()` uses the same cap, so a service that calls it can wait up to twice the cap while the gateway is hung: once in `flush_otel()` and once at exit. Keep that time plus your application's own shutdown time below the task's `stopTimeout`.

The ECS Terraform module needs two follow-up changes for this to hold in production:

- Set `awslogs` `mode = "non-blocking"` and a `max-buffer-size` explicitly, so stdout writes cannot stall if CloudWatch is unreachable. The module currently leaves `mode` to the account's `defaultLogDriverMode` setting (unknown).
- Lower the module's `otel_gateway.logs_timeout` default from 20 seconds to about 5.

### Lambda

Lambda freezes the process when the handler returns, so the background thread cannot be relied on. The outer handler wrapper flushes inside `finally` on every invocation (normal return, handled error or unhandled exception), after the Lambda instrumentor closes the invocation span:

1. If `context.get_remaining_time_in_millis()` is less than the export timeout plus `I_DOT_AI_OTEL_LAMBDA_FLUSH_MARGIN_MS` (`2250 ms` by default), the flush is skipped and records stay queued for the next invocation.
2. The flush runs on a worker thread and the handler waits at most the export timeout, so slow DNS or a hung connection cannot hold the response longer.
3. After a failure, a process-local circuit breaker skips flushes for `I_DOT_AI_OTEL_LAMBDA_CIRCUIT_BREAKER_SECONDS`. New records stay queued up to `OTEL_BLRP_MAX_QUEUE_SIZE`, then the oldest are dropped.
4. Once one batch fails, the rest of that flush fails immediately, so an outage costs at most one export timeout per flush.

In SDK 1.45, `BatchLogRecordProcessor.force_flush()` ignores its timeout argument; the exporter timeout bounds each batch. Keep `OTEL_BLRP_MAX_EXPORT_BATCH_SIZE` large enough that an invocation fits in one batch.

Delivery is at least once: a batch that reaches the gateway just as the client times out may be retried. Deduplicate on `trace_id` and `faas.invocation_id` if needed.

## Extending

To support a new framework, add an extra in `pyproject.toml` that pulls in its OpenTelemetry instrumentation package:

```toml
otel_native_<framework> = [
    "i-dot-ai-utilities[otel_native]",
    "opentelemetry-instrumentation-<framework>>=0.66b1,<0.67",
]
```

No code change is needed. `configure_otel()` enables every installed package registered under the `opentelemetry_instrumentor` entry point group, the same mechanism the `opentelemetry-instrument` CLI uses, and skips any whose target library is missing or the wrong version.

To support a new runtime, subclass `Runtime` in `_runtime.py`, override only the steps that differ (`apply_env_defaults`, `resource_attributes`, `log_record_processor`, `post_configure`), add it to `_RUNTIMES` in `_configuration.py`, and extend `detect_runtime()`.

## Module layout

| Module | Responsibility |
|---|---|
| `_configuration.py` | `configure_otel()`, runtime detection, root handler, process state |
| `_runtime.py` | `Runtime` base class, `LocalRuntime`, shared env defaults |
| `_lambda.py` | `LambdaRuntime`, handler wrapping, per-invocation flush |
| `_ecs.py` | `EcsRuntime`, Batch attributes, capped shutdown, `flush_otel()`, SIGTERM |
| `_export.py` | Failure-reporting exporter; Lambda circuit breaker and processor |
| `_attributes.py` | `i_ai` naming helpers, semantic convention registry, enforcement processor |
| `_instrumentation.py` | Entry-point based auto-instrumentation |
| `_stdout.py` | Single-line JSON stdout handler |
| `_emf.py` | CloudWatch EMF failure line |
| `_environment.py` | Environment variable parsing |

## Tests

```bash
make test-otel
```

The integration tests run each scenario in a subprocess against a loopback OTLP capture server and, for ECS, a fake task metadata endpoint. They cover outage, hung gateway, rejected exports, shutdown time, SIGTERM and stdout behaviour.
