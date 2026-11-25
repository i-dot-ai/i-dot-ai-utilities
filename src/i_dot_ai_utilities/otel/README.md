# OpenTelemetry (`i_dot_ai_utilities.otel`)

`i_dot_ai_utilities.otel` sends Python application logs and traces to the i.AI `otel-gateway` using the standard OpenTelemetry SDK. Call `configure_otel()` once at startup. The function detects the runtime, configures the SDK, attaches an OpenTelemetry handler to Python's root logger, and enables every installed instrumentation package. Application code then uses standard Python `logging`.

`configure_otel()` returns the process-wide `LoggerProvider` attached to Python's root logger. Calling `configure_otel()` again in the same process is idempotent: it returns the existing `LoggerProvider` without reconfiguring the SDK. On AWS Lambda, repeat calls still attempt to wrap the handler if an earlier call ran before the handler was defined.

This module is separate from `StructuredLogger` and the legacy `i_dot_ai_utilities.logging.otel` bridge, and it imports neither. Some other modules in this package still accept a `StructuredLogger`. See [Modules that still use `StructuredLogger`](#modules-that-still-use-structuredlogger).

## Install the package

Install `otel_native` for plain ECS, AWS Batch, or local runs. Each platform and framework extra (`otel_native_lambda`, `otel_native_botocore`, and `otel_native_fastapi`) already includes `otel_native`, so you do not need to list `otel_native` alongside them.

| Extra | Adds | Use for |
|---|---|---|
| `otel_native` | SDK, OTLP over HTTP exporter, AWS resource detectors, and X-Ray propagator | ECS, AWS Batch, and local runs with no framework |
| `otel_native_lambda` | `otel_native` and AWS Lambda instrumentation | Python Lambdas |
| `otel_native_botocore` | `otel_native` and botocore and boto3 instrumentation | Services and Lambdas that call AWS APIs |
| `otel_native_fastapi` | `otel_native` and FastAPI instrumentation | FastAPI services |

```bash
pip install --pre "i-dot-ai-utilities[otel_native_fastapi]"
```

The extras pin the OpenTelemetry SDK to one minor version (currently 1.45), because capped shutdown relies on an SDK internal attribute. Tests in `__tests__/test_configuration.py` (`TestSdkInternals`) fail if an SDK upgrade removes that attribute.

## Runtimes

`configure_otel()` selects a runtime from the environment. Pass `runtime="lambda"`, `runtime="ecs"`, or `runtime="local"` to override automatic detection.

| Runtime | Detected by | How telemetry exports |
|---|---|---|
| `lambda` | `AWS_LAMBDA_FUNCTION_NAME` | Synchronously at the end of each invocation, bounded by the export timeout, with an outage circuit breaker |
| `ecs` | `ECS_CONTAINER_METADATA_URI_V4` or `AWS_BATCH_JOB_ID` | On the SDK's background thread so requests never wait on the network |
| `local` | Neither variable above | Background thread over OTLP when an endpoint is set, or stdout only otherwise |

The `ecs` runtime covers ECS on Fargate, ECS on EC2 (including GPU instances), and AWS Batch jobs. All three expose the ECS task metadata endpoint, which `AwsEcsResourceDetector` reads for `aws.ecs.*` resource attributes. AWS Batch jobs also receive `i_ai.aws.batch.job.id`, `i_ai.aws.batch.job.attempt`, and `i_ai.aws.batch.job.queue`.

AWS Batch is assumed to set `ECS_CONTAINER_METADATA_URI_V4` in job containers on both EC2 and Fargate compute environments. Runtime detection also checks `AWS_BATCH_JOB_ID`, so the runtime remains `ecs` even if `ECS_CONTAINER_METADATA_URI_V4` is absent. In that case, only the `aws.ecs.*` attributes are missing.

## Configure FastAPI on ECS

Call `configure_otel()` before importing FastAPI. The FastAPI instrumentor replaces `fastapi.FastAPI` at import time, so an app created before `configure_otel()` is not instrumented.

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

Uvicorn's own server and access logs are not exported over OTLP. Uvicorn turns off propagation on its `uvicorn` and `uvicorn.access` loggers, so those records never reach the root handler and stay on uvicorn's console output only.

Under Gunicorn, keep `configure_otel()` at module level in the app module. With the default setting (no `--preload`), each worker imports the app and configures itself. If you use `--preload` or Celery prefork workers, avoid emitting OTLP logs before `fork()` so the parent process does not open TLS sockets before spawning workers. After `fork()`, `configure_otel()` resets failure-signal locks and closes pooled exporter transports, and the SDK restarts its export thread in each worker. A uvicorn worker without `--preload` flushes its queued logs when Gunicorn stops it (verified with Gunicorn 27.0 and uvicorn 0.54). Sync workers and `--preload` workers have not been verified for exit flushing, though their logs remain on stdout either way.

### Flush logs on shutdown

Call `flush_otel()` after `yield` in the lifespan, as shown in the example above. `flush_otel()` exports queued logs and traces and waits at most `I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS`, so a hung gateway cannot hold the task past `stopTimeout`. Lifespan shutdown runs after the server finishes its open requests, so the flush does not delay any response.

Some servers end the process without running Python's `atexit` hooks, which prevents the library's own exit flush from running. Without `flush_otel()`, logs queued since the last export (up to 1 second by default) miss the gateway when the task stops. Those logs still appear on stdout.

| Server | Exit flush runs without `flush_otel()` |
|---|---|
| `uvicorn main:app`, uvicorn before 0.47 | No |
| `uvicorn main:app`, uvicorn 0.47 or later | Yes |
| `uvicorn main:app --workers N` | No |
| Gunicorn with `uvicorn.workers.UvicornWorker` | Yes |

These results were verified with uvicorn 0.31.1, 0.46.0, 0.47.0, and 0.54.0, and Gunicorn 27.0 without `--preload`. The `--workers` row was verified on uvicorn 0.54.0 only. Other servers, such as Hypercorn and Granian, have not been verified. Calling `flush_otel()` is safe on every server.

### Start the server as the container's main process

ECS and AWS Batch send `SIGTERM` only to the container's main process (PID 1). If PID 1 is a shell script that starts the server in the background, the server never receives `SIGTERM`. The server keeps running until the task is killed at `stopTimeout`, and nothing flushes. Library code cannot fix this behaviour, so configure the container entrypoint correctly.

End an entrypoint script with `exec` so the server replaces the shell and becomes PID 1:

```bash
#!/bin/bash
set -e
alembic upgrade head
exec uvicorn backend.main:app --host 0.0.0.0 --port 8080
```

Do not start the server with `uvicorn ... &` followed by `wait`. In a Dockerfile, use the JSON form, `CMD ["uvicorn", "backend.main:app"]`, because the shell form runs the command under `/bin/sh -c`. A `uv run` prefix is safe because `uv run` forwards `SIGTERM` to the server (verified with uv 0.12.3).

## Configure Django

There is no `otel_native_django` extra yet. `configure_otel()` still enables Django instrumentation whenever `opentelemetry-instrumentation-django` is installed, which the `all` and legacy `otel_django` extras include.

Set `DJANGO_SETTINGS_MODULE` before calling `configure_otel()`. If `DJANGO_SETTINGS_MODULE` is unset, the instrumentor configures Django with empty settings, and the project's own settings never load. To disable Django instrumentation, set `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS=django`.

## Configure AWS Batch jobs and plain scripts

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

When ECS or AWS Batch stops a task, it sends `SIGTERM`. Python's default reaction terminates the process without running `atexit`, which drops queued logs. On the `ecs` runtime, `configure_otel()` installs a `SIGTERM` handler that exits with code `143` (`128 + SIGTERM`) via `sys.exit`, so `finally` blocks and the log flush run. `configure_otel()` installs this handler only when no custom `SIGTERM` handler is already set.

A web server can install its own `SIGTERM` handler first, or exit without running `atexit`. For a web service, call `flush_otel()` as described in [Flush logs on shutdown](#flush-logs-on-shutdown).

Python waits for non-daemon threads to finish before running `atexit` hooks. A background thread that never stops holds the process until ECS kills it, and queued logs miss OTLP export. Create background threads with `daemon=True`, or stop them in a `finally` block.

## Configure AWS Lambda

Define the handler first, then call `configure_otel()` at the bottom of the handler module. `LambdaRuntime` checks `ORIG_HANDLER` before `_HANDLER` (so Lambda wrapper scripts that reassign `_HANDLER` still work) and wraps that function without requiring a Lambda layer:

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


# Call after defining lambda_handler so the instrumentor wraps ORIG_HANDLER or _HANDLER.
configure_otel()
```

If `configure_otel()` runs before the handler is defined, it cannot wrap the handler, so nothing flushes at the end of each invocation. In that case, `configure_otel()` writes `call configure_otel() after defining the handler` to stderr and keeps the runtime's stdout logging, regardless of `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS`.

## Name attributes

Attribute names are shared across every service that sends telemetry to `otel-gateway`. Choose names using these rules, in order:

1. Use an OpenTelemetry semantic convention attribute when one fits. Look up standard names in the [attribute registry](https://opentelemetry.io/docs/specs/semconv/registry/attributes/), which logs, traces, metrics, and resources all share. For example, use `user.email` for an email address, `url.full` for a URL, and `error.type` for an error class.
2. Otherwise, place the name under the `i_ai` namespace as `i_ai.<service>.<object>.<property>`. For example, use `i_ai.auth.decision.reason`.
3. Never add custom names under an OpenTelemetry namespace such as `user.`, `http.`, `app.`, or `service.`, even if a name such as `user.my_field` reads clearly. OpenTelemetry can define that name later with a different meaning.
4. Write each level in lowercase `snake_case`, separated by dots. For example, use `i_ai.auth.token.key_id`, not `i_ai.auth.tokenKeyId`.

The registry marks each attribute with a stability level. `Stable` names will not change. `Development` names, such as `user.email`, may be renamed in a later conventions release. Read the full rules in the [OpenTelemetry naming specification](https://opentelemetry.io/docs/specs/semconv/general/naming/).

### Attribute helpers

```python
from i_dot_ai_utilities.otel import I_AI_NAMESPACE, iai_attribute, validate_attributes

iai_attribute("auth.decision.reason")  # "i_ai.auth.decision.reason"
iai_attribute("auth.tokenKeyId")       # raises ValueError

validate_attributes({"user.email": email, iai_attribute("auth.token.key_id"): key_id})  # passes
validate_attributes({"application": "casework"})  # raises ValueError listing "application"
```

Define each name once as a module-level constant, as shown in the FastAPI and Lambda examples above. A bad name then fails at import time, where tests catch it immediately. Calling `iai_attribute()` inline inside a log call (`extra={iai_attribute("..."): value}`) raises at runtime instead, dropping the log line and propagating the exception into the request.

`iai_attribute()` requires at least `<service>.<property>`. `validate_attributes()` accepts any name in the semantic convention registry bundled with the installed SDK, including template names such as `http.request.header.<key>` with any key (`http.request.header.content-type`), plus any well-formed `i_ai.` name.

### Attribute enforcement

`AttributeNamespaceProcessor` checks every log record, whether or not you used the helpers. Before export, it renames any non-compliant key to `i_ai.unnamespaced.<key>` and writes a warning naming that key to stderr once per process. No data is lost, and you can locate non-compliant keys in the backend. The stdout copy of a log record keeps the original key.

Set `I_DOT_AI_OTEL_STRICT_ATTRIBUTES=true` in tests or CI to raise `ValueError` from the logging call instead of renaming keys. Strict mode works even when export is off (`OTEL_LOGS_EXPORTER=none`), which is how most test suites run. Known third-party framework loggers (such as `django.request`, which attaches `status_code` and `request`) are still renamed to `i_ai.unnamespaced.*` instead of raising so framework logs do not break test suites.

On Lambda, the runtime adds `aws_request_id` to every record. The processor exports it under the semantic convention `faas.invocation_id`.

Standard attributes added by the SDK, such as `code.file.path`, `code.function.name`, `code.line.number`, and `exception.*`, are semantic conventions and pass unchanged.

Never log bearer tokens, `Authorization` headers, JWT segments, or raw request bodies. This library does not copy the Lambda event or HTTP request onto log records.

### Names the gateway masks

`otel-gateway` replaces the value of any attribute that matches its redaction patterns. Avoid these patterns in attribute names that hold non-secret data:

- Case-insensitive key matches: any key containing `password`, `passwd`, `secret`, `authorization`, `cookie`, `api_key` (`api-key` or `apikey`), or `access_key` (`access-key` or `accesskey`). For example, the value of `i_ai.storage.access_key_id` arrives masked.
- Case-insensitive suffix matches: any key ending in `token` preceded only by ASCII letters after the start of the key or a `.`, `_`, or `-` separator, such as `i_ai.auth.session_token`. Counters such as `gen_ai.usage.input_tokens` are not affected.

Regardless of key name, the gateway also masks values that match any of these patterns:

- AWS access key IDs (`AKIA` or `ASIA` followed by 16 alphanumeric characters).
- JWTs.
- Bearer tokens.
- `sk-` API keys (20 or more alphanumeric characters after `sk-`).

The authoritative redaction patterns live in `collector/config.yaml.tftpl` in the `core-logging` repository.

## Service name

`configure_otel()` resolves `service.name` in this order:

1. `OTEL_SERVICE_NAME`, or `service.name` in `OTEL_RESOURCE_ATTRIBUTES`. The shared Terraform modules set `OTEL_SERVICE_NAME` when `otel_gateway.enabled = true`.
2. The `service_name="..."` argument passed to `configure_otel()`. Use this argument only when nothing sets the environment variable, such as during a local run.
3. Neither set: if an OTLP endpoint is configured, `configure_otel()` warns once on stderr, defaults `OTEL_LOGS_EXPORTER` and `OTEL_TRACES_EXPORTER` to `none`, and writes logs to stdout only.

The environment takes precedence because the gateway's `filter/service_allowlist` processor silently drops logs and traces whose `service.name` is not on `allowed_service_names`, while still returning HTTP `200`. The name set by Terraform matches the allow-list. For the same reason, `configure_otel()` disables export when no service name is set, because the SDK would otherwise send `unknown_service` and the gateway would drop it.

## Deployment environment

`deployment.environment.name` defaults to the `ENVIRONMENT` environment variable, which i.AI services already set. Loki indexes `deployment.environment.name` as a label. Set `deployment.environment.name` in `OTEL_RESOURCE_ATTRIBUTES` to override it.

## Environment variables

`configure_otel()` applies defaults using `os.environ.setdefault`, so any variable you set in the environment always wins.

### All runtimes

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_SERVICE_NAME` | _(from Terraform)_ | Service name. Takes precedence over the `service_name` argument |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | _(from Terraform SSM)_ | Base OTLP over HTTP URL, such as `https://otel-gateway.dev.i.ai.gov.uk:4318`. The SDK appends `/v1/logs` and `/v1/traces` |
| `OTEL_EXPORTER_OTLP_LOGS_ENDPOINT` | _(unset)_ | Logs-specific endpoint override, used verbatim |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | _(unset)_ | Traces-specific endpoint override, used verbatim |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | OTLP transport. `http/protobuf` is the only supported value |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `5` on `ecs`, `2` on `lambda` _(from Terraform)_ | Per-batch OTLP export timeout in seconds for logs and traces (overridden by `OTEL_EXPORTER_OTLP_LOGS_TIMEOUT` or `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT`) |
| `OTEL_EXPORTER_OTLP_COMPRESSION` | `gzip` | OTLP request body compression (`gzip`, `deflate`, or `none`) |
| `OTEL_EXPORTER_OTLP_LOGS_COMPRESSION` | _(unset)_ | Logs-specific compression override |
| `OTEL_LOGRECORD_ATTRIBUTE_VALUE_LENGTH_LIMIT` | `16384` (when `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT` is unset) | Maximum characters per log attribute value to protect Loki's 64 KiB structured metadata limit. Stack traces (tracebacks) longer than this lose their end in Grafana and Loki, but stay complete on stdout |
| `OTEL_EXPORTER_OTLP_HEADERS` | _(unset)_ | Comma-separated `key=value` headers |
| `OTEL_RESOURCE_ATTRIBUTES` | _(unset)_ | Extra resource attributes, such as `service.namespace=casework` |
| `ENVIRONMENT` | _(set by the service)_ | Default value for `deployment.environment.name` |
| `OTEL_LOGS_EXPORTER` | `otlp` when a logs endpoint and a service name are set, otherwise `none` | Logs exporter |
| `OTEL_TRACES_EXPORTER` | `otlp` when a traces endpoint and a service name are set, otherwise `none` | Traces exporter. Set to `none` to turn span export off while keeping log trace correlation |
| `OTEL_METRICS_EXPORTER` | `none` | Disabled because `otel-gateway` has no metrics pipeline (it carries logs and traces only) |
| `OTEL_PYTHON_DISABLED_INSTRUMENTATIONS` | _(unset)_ | Comma-separated instrumentations to skip, such as `fastapi`, or `*` to disable all |
| `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS` | `true` | Write application logs to stdout alongside OTLP |
| `I_DOT_AI_OTEL_STRICT_ATTRIBUTES` | `false` | Raise `ValueError` on non-compliant attribute names instead of renaming them |

`I_DOT_AI_OTEL_LAMBDA_KEEP_STDOUT_LOGS` is the earlier name for `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS` and remains supported. `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS` takes precedence if both are set.

### Protocol

OTLP over HTTP (`http/protobuf`) is the only supported protocol. The extras install only the HTTP exporter, and every outage, shutdown, and `SIGTERM` test runs over HTTP. The SDK rejects `http/json`, and it rejects `grpc` because the gRPC exporter is not installed. When the SDK rejects the protocol, `configure_otel()` warns once on stderr, turns export off, and writes logs to stdout only, so a bad setting cannot stop the application from starting.

On `lambda` and `ecs`, `OTEL_PYTHON_ID_GENERATOR` defaults to `xray` and `OTEL_PROPAGATORS` defaults to `xray,tracecontext,baggage`, producing X-Ray compatible trace IDs.

### `ecs` runtime

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_EXPORTER_OTLP_LOGS_TIMEOUT` | `5` (unset when `OTEL_EXPORTER_OTLP_TIMEOUT` is set) | Per-batch logs export timeout in seconds |
| `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` | `5` (unset when `OTEL_EXPORTER_OTLP_TIMEOUT` is set) | Per-batch traces export timeout in seconds |
| `I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS` | `5000` | Maximum wait time in milliseconds for shutdown and each `flush_otel()` call |
| `I_DOT_AI_OTEL_MAX_CONNECTION_AGE_SECONDS` | `300` | Seconds before pooled HTTP keep-alive connections close so active ECS tasks rebalance behind the gateway NLB (`0` disables recycling) |
| `OTEL_BLRP_SCHEDULE_DELAY` | `1000` (SDK default) | Background export interval in milliseconds |
| `OTEL_BLRP_MAX_QUEUE_SIZE` | `2048` (SDK default) | Maximum log records buffered before the oldest are dropped |

### `lambda` runtime

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_EXPORTER_OTLP_LOGS_TIMEOUT` | `2` (unset when `OTEL_EXPORTER_OTLP_TIMEOUT` is set) | Per-batch logs export timeout in seconds. The Python SDK reads this value in seconds, so `2` means 2 seconds, not 2 milliseconds |
| `OTEL_EXPORTER_OTLP_TRACES_TIMEOUT` | `2` (unset when `OTEL_EXPORTER_OTLP_TIMEOUT` is set) | Per-batch traces export timeout in seconds |
| `OTEL_INSTRUMENTATION_AWS_LAMBDA_FLUSH_TIMEOUT` | `0` | Flush timeout in milliseconds passed to `AwsLambdaInstrumentor`. Setting `0` disables the instrumentor's blocking flush on the handler thread so this library's bounded daemon worker flushes spans and logs under the circuit breaker |
| `OTEL_BLRP_MAX_EXPORT_BATCH_SIZE` | `512` | Maximum records per OTLP POST so a normal invocation exports in one batch |
| `OTEL_BLRP_MAX_QUEUE_SIZE` | `2048` | Maximum records buffered across invocations |
| `I_DOT_AI_OTEL_LAMBDA_FLUSH_MARGIN_MS` | `250` | Remaining-time buffer in milliseconds required on top of the export timeout before flushing |
| `I_DOT_AI_OTEL_LAMBDA_CIRCUIT_BREAKER_SECONDS` | `30` | Seconds to skip synchronous flushes after an export failure |

## Stdout logging

Application logs go to stdout as well as OTLP by default on every runtime so CloudWatch keeps a copy during a gateway outage. A successful response from the gateway means the gateway queued the batch, not that it persisted the data to storage, so the gateway guidance is to keep a CloudWatch copy. The trade-off is CloudWatch ingestion cost for every log line.

On `ecs` and `local`, stdout writes one JSON object per line. Each line contains `timestamp`, `level`, `logger`, `message`, `trace_id` and `span_id` inside a span, every `extra=` attribute, and `exception` when present. Writing a single line per record prevents `awslogs` from splitting a multi-line traceback into separate events.

`configure_otel()` adds the stdout handler only when the root logger has no other handlers. If your application already configures console logging, `configure_otel()` leaves that configuration untouched. Set `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS=false` to send logs to OTLP only. When no OTLP endpoint is configured, or when `OTEL_LOGS_EXPORTER=none`, stdout stays enabled regardless so turning export off never silences logs.

On Lambda, the runtime's own `awslambdaric` stdout handlers already write to CloudWatch, so `configure_otel()` leaves them in place and adds no new stdout handler. With `I_DOT_AI_OTEL_KEEP_STDOUT_LOGS=false`, `configure_otel()` removes those runtime handlers only after wrapping the Lambda handler and only while OTLP export is enabled.

## Modules that still use `StructuredLogger`

`auth`, `metrics`, `file_store`, and `litellm` take a `StructuredLogger` in their constructors. `StructuredLogger` writes straight to stdout, so logs from those modules reach CloudWatch but not `otel-gateway`. Migrating those modules to standard Python `logging` is separate work.

Passing a standard `logging.Logger` works only for `metrics`, which calls only `.exception()`. The other modules pass keyword arguments such as `logger.info("... {key}", key=key)`, which standard `logging` rejects with `TypeError`. `auth` passes keyword arguments only in `debug` calls, so it works until the logger level is `DEBUG`.

## Log levels

`configure_otel()` lowers the root logger from Python's default `WARNING` to `INFO` so `logger.info(...)` works without extra setup. Because `configure_otel()` cannot distinguish Python's default from an intentional `WARNING`, set `WARNING` after calling `configure_otel()` if you want that level.

The OpenTelemetry handler remains attached to the root logger even if your application later calls `logging.config.dictConfig`, `fileConfig`, or `basicConfig`. Those calls can still change the root level. Alembic's default `alembic.ini` sets the root level to `WARN`, so `INFO` logs stop after an in-process migration runs unless you update `alembic.ini`.

`configure_otel()` sets the `botocore`, `boto3`, and `urllib3` loggers to `WARNING` so their `INFO` messages are not exported, unless your application set their level first.

Logs from `opentelemetry.*` loggers are never exported, which prevents exporter errors from looping back into the exporter.

## Failure behaviour

A gateway that is down, slow, hung, or rejecting requests must never break or slow the application.

### Failure signal

When an export fails, the library writes one JSON line to stdout that is also a CloudWatch Embedded Metric Format (EMF) metric: `level="WARNING"`, `event="otel_log_export_failed"`, and `ExportFailures = 1` in namespace `i-dot-ai/otel`. The payload omits endpoints, headers, bodies, and exception text. CloudWatch Logs extracts this metric from both Lambda logs and ECS `awslogs`.

| Runtime | Dimension | Rate |
|---|---|---|
| `lambda` | `FunctionName` | Once per failed or skipped flush so the alarm keeps firing throughout an outage |
| `ecs` | `ServiceName` | At most once per 60 seconds per process |

On Lambda, enable `export_failures_threshold = 0` on `modules/observability/lambda-alarms` to alert on export failures in both `direct` and `extension` modes. That alarm sums the library's `ExportFailures` EMF metric and a metric filter on the collector extension's `"Exporting failed"` log lines. In `mode = "extension"`, the library exports to `localhost`, so gateway outages surface through the collector's log lines rather than the library metric.

### Containers (`ecs`)

Application requests only append log records and spans to bounded in-memory queues, and the SDK's background threads handle network calls. During an outage, the log queue fills to `OTEL_BLRP_MAX_QUEUE_SIZE` and drops the oldest records from OTLP. Those records remain on stdout.

The SDK's default `atexit` shutdown waits up to 30 seconds for each export thread, which consumes the entire default ECS `stopTimeout` (30 seconds) and leaves no time for application shutdown. `configure_otel()` replaces that hook with a shutdown capped at `I_DOT_AI_OTEL_SHUTDOWN_TIMEOUT_MS`. `flush_otel()` uses the same cap, so a service that calls `flush_otel()` can wait up to twice the cap while the gateway is hung: once in `flush_otel()` and once at exit. Keep that combined time plus your application's own shutdown time below the task's `stopTimeout`.

The shared ECS Terraform module configures `awslogs` with `mode = "non-blocking"` and `max-buffer-size = "25m"` so stdout writes cannot stall when CloudWatch is unreachable. Both the ECS and Batch Terraform modules default `otel_gateway.timeout` to `5` seconds to match the shutdown cap.

### Lambda

AWS Lambda freezes the execution environment when the handler returns, so `LambdaRuntime` buffers records in a bounded in-memory queue with no background export thread. The outer handler wrapper flushes inside `finally` on every invocation (normal return, handled error, or unhandled exception), after the Lambda instrumentor closes the invocation span:

1. If no records are queued, the flush returns immediately without starting a thread or touching the network.
2. If `context.get_remaining_time_in_millis()` is less than the export timeout plus `I_DOT_AI_OTEL_LAMBDA_FLUSH_MARGIN_MS` (`2250 ms` by default), the wrapper skips the flush and leaves records queued for the next invocation.
3. The flush runs on a daemon worker thread, and the handler waits at most the export timeout (plus a small join overhead), so slow DNS or a hung connection cannot hold the response longer.
4. After an export failure, a process-local circuit breaker skips flushes for `I_DOT_AI_OTEL_LAMBDA_CIRCUIT_BREAKER_SECONDS`. New records stay queued up to `OTEL_BLRP_MAX_QUEUE_SIZE`, then the oldest records are dropped.
5. Once one batch fails, the rest of that flush fails immediately, so an outage costs at most one export timeout per flush.

Keep `OTEL_BLRP_MAX_EXPORT_BATCH_SIZE` large enough that a typical invocation fits in one batch, because the exporter timeout bounds each batch individually.

Delivery is at least once: a batch that reaches the gateway just as the client times out may be retried. Deduplicate on `trace_id` and `faas.invocation_id` if needed.

## Extend the module

To support a new framework, add an extra in `pyproject.toml` that includes `otel_native` and the framework's OpenTelemetry instrumentation package:

```toml
otel_native_<framework> = [
    "i-dot-ai-utilities[otel_native]",
    "opentelemetry-instrumentation-<framework>>=0.66b1,<0.67",
]
```

No code change is needed. `configure_otel()` enables every installed package registered under the `opentelemetry_instrumentor` entry point group (the same mechanism used by the `opentelemetry-instrument` CLI) and skips any package whose target library is missing or incompatible.

To support a new runtime, subclass `Runtime` in `_runtime.py`, override only the steps that differ (`apply_env_defaults`, `resource_attributes`, `log_record_processor`, `span_processor`, and `post_configure`), register it in `_load_runtime_class()` in `_configuration.py`, and update `detect_runtime()`.

## Module layout

| Module | Responsibility |
|---|---|
| `_configuration.py` | `configure_otel()`, runtime detection, root handler, and process state |
| `_runtime.py` | `Runtime` base class, `LocalRuntime`, and shared environment defaults |
| `_lambda.py` | `LambdaRuntime`, handler wrapping, and per-invocation flush |
| `_ecs.py` | `EcsRuntime`, AWS Batch attributes, capped shutdown, `flush_otel()`, and `SIGTERM` handling |
| `_export.py` | Failure-reporting exporters, Lambda circuit breaker, and Lambda processors |
| `_attributes.py` | `i_ai` naming helpers, semantic convention registry, and enforcement processor |
| `_instrumentation.py` | Entry-point auto-instrumentation |
| `_stdout.py` | Single-line JSON stdout handler |
| `_emf.py` | CloudWatch EMF failure line |
| `_environment.py` | Environment variable parsing |

## Run tests

```bash
make test-otel
```

The integration tests run each scenario in a subprocess against a loopback OTLP capture server and, for ECS, a fake task metadata endpoint, because the OpenTelemetry SDK allows global providers to be set only once per Python process. In-process unit tests construct local `LoggerProvider` and `TracerProvider` instances directly (see `__tests__/conftest.py`).
