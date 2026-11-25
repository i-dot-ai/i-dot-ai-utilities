# Structured logging

> **Migration notice:** For new services on ECS, Lambda, AWS Batch, and FastAPI, use `i_dot_ai_utilities.otel` instead (see the [OpenTelemetry guide](../otel/README.md)). `StructuredLogger` writes to stdout only and does not export to `otel-gateway`. The legacy `i_dot_ai_utilities.logging.otel` bridge and the Django middleware documented below will be removed.

## Usage

### Create a logger

Instantiate `StructuredLogger` to format logs for downstream consumers:

```python
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger

logger = StructuredLogger()

logger.info("Service started")
```

For production services, configure console text output locally and enriched JSON output on ECS (Fargate):

```python
import os

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.logging.types.enrichment_types import ExecutionEnvironmentType
from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat

environment = os.environ.get("ENVIRONMENT")
logger_environment = ExecutionEnvironmentType.LOCAL if environment == "LOCAL" else ExecutionEnvironmentType.FARGATE
logger_format = LogOutputFormat.TEXT if environment == "LOCAL" else LogOutputFormat.JSON

logger = StructuredLogger(
    level="info",
    options={
        "execution_environment": logger_environment,
        "log_format": logger_format,
    },
)
```

### Create log messages

Once you initialise the logger, emit log messages in three ways depending on the fields you need.

Log a plain string literal:

```python
logger.info("A thing happened")
```

Attach structured context fields to the log record:

```python
logger.info("Another thing happened", thing_id=12345, user_logged_in=True)
```

Interpolate values into the message while also capturing them as separate structured fields:

```python
logger.info("Yet another thing occurred for user {email} with id {id}", email=user_email, id=user_id)
```

Do not use f-strings in log messages. Passing keyword arguments lets `StructuredLogger` interpolate the message and preserve each field for downstream indexing.

When called inside an `except` block, `logger.exception()` logs the message and attaches the exception traceback automatically:

```python
logger.exception("Something went wrong when user {email} logged in", email=email)
```

### Refresh the logger

Call `refresh_context()` at the entry point of each API request or invocation. This call generates a new context ID and clears custom fields added earlier by enrichers or `set_context_field()`:

```python
@app.post("/do/the/thing")
async def do_the_thing(request: Request):
    logger.refresh_context()

    do_stuff()
```

Django services using `StructuredLoggingMiddlewareOTel` do not need to call `refresh_context()` manually because the middleware refreshes context per request.

### Set execution environment type and log format

#### Environment type

Configure the execution environment with the `execution_environment` option when instantiating `StructuredLogger`. Accepted values are defined in the `ExecutionEnvironmentType` enum in [enrichment_types.py](./types/enrichment_types.py). The logger extracts metadata from the configured execution environment and adds it to each log record.

#### Log format

Configure the output format with the `log_format` option when instantiating `StructuredLogger`. Accepted values are defined in the `LogOutputFormat` enum in [log_output_format.py](./types/log_output_format.py).

Use `LogOutputFormat.TEXT` for readable console output during local development. Use `LogOutputFormat.JSON` on deployed environments so downstream log pipelines can parse structured records.

### Implement persistent context

Use context enrichers to extract request or runtime metadata and attach it to subsequent log messages:

```python
from fastapi import FastAPI, Request

from i_dot_ai_utilities.logging.types.enrichment_types import ContextEnrichmentType

app = FastAPI()


@app.get("/")
async def root(request: Request):
    logger.refresh_context(
        context_enrichers=[
            {
                "type": ContextEnrichmentType.FASTAPI,
                "object": request,
            }
        ]
    )

    do_stuff()
```

This example extracts fields from the FastAPI request object (such as query string, path, and user agent) and attaches them to every subsequent log message until the next `refresh_context()` call.

Each enricher entry accepts `type` and `object`. `type` is a `ContextEnrichmentType` enum member (see [enrichment_types.py](./types/enrichment_types.py)). `object` is the runtime object passed to the enricher:

| Name | Input object | Extracted fields |
|---|---|---|
| FastAPI and Starlette | [Request object](https://fastapi.tiangolo.com/advanced/using-request-directly/#details-about-the-request-object) | [FastApiRequestMetadata](./types/fastapi_enrichment_schema.py#L31) |
| Lambda | [Lambda context object](https://docs.aws.amazon.com/lambda/latest/dg/python-context.html) | [LambdaContextMetadata](./types/lambda_enrichment_schema.py) |

Django services do not use a `ContextEnrichmentType`. When using the legacy Django middleware below, HTTP request attributes live on OpenTelemetry spans instead.

### Add custom context fields

Call `set_context_field()` to attach custom fields to every subsequent log message until the next `refresh_context()` call:

```python
@app.post("/login")
async def login(request: Request):
    logger.refresh_context()

    user_email = get_user_info(request)

    logger.info("User {email} started login process", email=user_email)
    logger.set_context_field("email", user_email)
```

Downstream logging systems index fields added with `set_context_field()` so you can search on them.

## Django structured logging middleware

> **Deprecation notice:** `i_dot_ai_utilities.logging.otel`, `StructuredLoggingMiddlewareOTel`, and `DjangoUserIdMiddleware` belong to the legacy structlog OpenTelemetry bridge and will be removed. Do not adopt them for new services.

`StructuredLoggingMiddlewareOTel` is an OpenTelemetry-backed Django middleware that refreshes per-request context and logs request and response lifecycle events, removing the need for manual `refresh_context()` calls in views.

HTTP request attribute extraction (method, path, route, status code, user agent, and client address) runs in `opentelemetry-instrumentation-django`, which creates a server span per request carrying those attributes. The middleware handles only log-lifecycle concerns that cannot live on a span: `request_started`, `request_completed`, and `request_failed` events, `duration_ms`, status-driven log levels, per-hop `request_id`, path exclusions, header allowlisting, and request scope ownership.

Trace correlation on log records comes from a structlog processor reading the active span on every event, so any `logger.info(...)` call inside a view receives `trace_id`, `span_id`, and `trace_flags`.

Install `django` (Django and the middleware) and `otel_django` (the OpenTelemetry bootstrap and Django auto-instrumentation). Because these extras ship as pre-releases, pass `--pre` or pin the exact pre-release version:

```bash
pip install --pre "i-dot-ai-utilities[django,otel_django]"
```

> **Pre-release channels:** The command above installs from production PyPI. If a pre-release was published to Test PyPI instead, point your installer at Test PyPI with production PyPI as a fallback index and pin the exact timestamped version. See [CI and releases](../../../README.md#ci-and-releases) in the root README for the full per-channel install commands.

Configure OpenTelemetry once at startup in `wsgi.py`, `asgi.py`, or `AppConfig.ready()`:

```python
from i_dot_ai_utilities.logging.otel import configure_otel_for_django

configure_otel_for_django(service_name="my-service")
```

When `OTEL_EXPORTER_OTLP_ENDPOINT` is set, `configure_otel_for_django()` automatically creates an `OTLPSpanExporter` targeting `<endpoint>/v1/traces` (using `OTEL_EXPORTER_OTLP_HEADERS` when set). When both `span_exporter` and `OTEL_EXPORTER_OTLP_ENDPOINT` are unset, it falls back to `ConsoleSpanExporter` for local development (unless `OTEL_REQUIRE_ENDPOINT` is enabled, in which case it raises `RuntimeError`). Pass `span_exporter` explicitly to override the default exporter:

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

from i_dot_ai_utilities.logging.otel import configure_otel_for_django

configure_otel_for_django(
    service_name="my-service",
    span_exporter=OTLPSpanExporter(endpoint="https://otel-collector.example/v1/traces"),
)
```

Environment variables supported by `i_dot_ai_utilities.logging.otel`:

| Variable | Purpose |
|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector HTTP base URL, such as `http://nlb:4318` |
| `OTEL_REQUIRE_ENDPOINT` | Set to `1` so a missing endpoint raises `RuntimeError` instead of falling back to console export |
| `OTEL_EXPORTER_OTLP_HEADERS` | Comma-separated `key=value` headers (for example `Authorization=Bearer ...`), applied to the trace, metric, and log exporters |

After constructing `StructuredLogger`, call `ensure_structlog_otel_processors()` from `i_dot_ai_utilities.logging.otel` so the trace-context and OTLP log processors survive logger reconfiguration. Legacy callers on this bridge path can also call `configure_otel_for_lambda()` and `force_flush_otel()` before exit. For new Python Lambdas targeting `otel-gateway`, use `i_dot_ai_utilities.otel` instead (see [OpenTelemetry-native logging](#opentelemetry-native-logging-i_dot_ai_utilitiesotel)).

If you do not call `configure_otel_for_django()` at startup, `StructuredLoggingMiddlewareOTel` emits a one-shot `WARNING` at worker boot (`structured_logging_middleware_otel_tracer_provider_missing`) pointing to the fix so missing trace correlation is immediately visible.

Add the middleware to `settings.MIDDLEWARE` after `SecurityMiddleware` and `AuthenticationMiddleware`, and before application-specific middleware so timing and logging wrap as much of the request lifecycle as possible:

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "i_dot_ai_utilities.logging.middleware.django_otel.StructuredLoggingMiddlewareOTel",
    # Add your other middleware here.
]
```

`DjangoInstrumentor` inserts its own middleware at the front of the chain automatically. Do not add it manually.

### Wire the structlog processors

`configure_otel_for_django()` can insert both `otel_trace_context_processor` and `otel_otlp_log_emitter_processor` into your structlog processor list before the renderer:

```python
import structlog

from i_dot_ai_utilities.logging.otel import configure_otel_for_django

processors = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    structlog.processors.JSONRenderer(),
]
configure_otel_for_django(
    service_name="my-service",
    structlog_processors=processors,  # Mutated in place.
)
structlog.configure(processors=processors)
```

To wire trace context manually without `structlog_processors`, add `otel_trace_context_processor` before the renderer, or call `insert_trace_processor(processors)` from `i_dot_ai_utilities.logging.otel` to insert both processors before the renderer:

```python
import structlog

from i_dot_ai_utilities.logging.otel import otel_trace_context_processor

processors = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    otel_trace_context_processor,  # Place before the renderer.
    structlog.processors.JSONRenderer(),
]
```

### Settings

All settings are optional. The middleware reads settings with `getattr(settings, ...)` and uses defaults for any setting you omit.

| Setting | Type | Default | Purpose |
|---|---|---|---|
| `I_DOT_AI_LOGGER` | Logger object or zero-argument callable | Bare `structlog.get_logger(__name__)` wrapped to satisfy the six-method contract (`info`, `warning`, `error`, `exception`, `refresh_context`, and `set_context_field`) | Logger used by the middleware. Dotted import strings are rejected to prevent arbitrary boot-time imports. Import your logger in `settings.py` and assign it directly, or pass a zero-argument factory |
| `I_DOT_AI_LOGGING_MIDDLEWARE_ENABLED` | `bool` | `True` | Set to `False` to disable the middleware. When disabled, it raises `MiddlewareNotUsed` at startup |
| `I_DOT_AI_LOGGING_EXCLUDED_PREFIXES` | Iterable of `str` | Health-check prefixes listed below | Skips all log events for paths matching these prefixes |
| `I_DOT_AI_LOGGING_EXCLUDED_REGEXES` | Iterable of `str` or `re.Pattern` | `()` | Additional regex path exclusions, compiled once at startup |
| `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` | Iterable of `str` | `()` | Header names to bind to log context (truncated to 512 characters). Explicit allowlist only, never a denylist. A built-in denylist (`Authorization`, `Cookie`, `Set-Cookie`, `Proxy-Authorization`, `X-CSRFToken`, `X-CSRF-Token`, `WWW-Authenticate`, and `X-API-Key`) always applies on top. Denied headers are filtered out and logged as a startup warning |

Default excluded prefixes skip high-volume health probes:

- `/health/`
- `/healthz`
- `/api/health/`
- `/api/health/live`
- `/api/health/ready`

### Example configuration

```python
# settings.py
from myapp.logging import build_logger  # Import directly; do not use a dotted string.

I_DOT_AI_LOGGER = build_logger()  # Or pass the zero-argument callable: build_logger
I_DOT_AI_LOGGING_EXCLUDED_PREFIXES = (
    "/healthz",
    "/api/internal/",
)
I_DOT_AI_LOGGING_HEADER_ALLOWLIST = ("X-Tenant-ID",)
```

### Emitted log events

| Event | When | Level |
|---|---|---|
| `structured_logging_middleware_otel_active` | Once per worker process at startup | `info` |
| `structured_logging_middleware_otel_forbidden_headers_rejected` | At startup only when `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` contains forbidden headers that were removed | `warning` |
| `structured_logging_middleware_otel_tracer_provider_missing` | At startup only when no SDK `TracerProvider` is installed (for example, when `configure_otel_for_django()` was not called) | `warning` |
| `request_started` | Entering the view chain for non-excluded paths | `info` |
| `request_completed` | View returned normally or raised `Http404` | `info` (2xx and 3xx), `warning` (4xx including `Http404`), `error` (5xx) |
| `request_failed` | View raised any unhandled exception other than `Http404` | `error` |

The middleware logs unhandled exceptions with full tracebacks via `logger.exception(...)` and re-raises them with a bare `raise`, preserving the original traceback for error trackers, Django REST framework, and the debug toolbar. `Http404` is treated as ordinary 4xx traffic: it logs at `warning` with status `404`, event name `request_completed`, and no traceback.

### Log record schema

The log schema is intentionally narrow because HTTP request context lives on the OpenTelemetry span rather than the log line. Every event includes `logging_schema_version = "1.0"` so consumers can detect breaking schema changes.

| Field | Source | Type | Notes |
|---|---|---|---|
| `logging_schema_version` | Middleware | `str` | Always `"1.0"` in this release |
| `http.response.status_code` | Middleware | `int` | Captured after the view returns. Synthesised to `500` on an unhandled exception and `404` on `Http404` |
| `error.type` | Middleware | `str`, optional | Status code string (such as `"404"` or `"500"`) on 4xx and 5xx responses and `Http404`. Fully qualified exception class name (such as `"builtins.RuntimeError"`) on unhandled exceptions. Absent on 2xx and 3xx responses, following OpenTelemetry HTTP semantic conventions |
| `exception.type` | Middleware | `str`, optional | `type(exc).__name__` on `request_failed` and on `Http404` `request_completed` events |
| `http.request.header.*` | Middleware | `str` | Emitted only for headers listed in `I_DOT_AI_LOGGING_HEADER_ALLOWLIST`, lowercased with hyphens replaced by underscores and values capped at 512 characters |
| `duration_ms` | Middleware | `int` | `time.monotonic()` delta, clamped to at least `0` |
| `request_id` | Middleware | `str` | Always a fresh per-hop UUID4 hex string (32 characters), distinct from `trace_id` and from any inbound correlation ID |
| `upstream_request_id` | Middleware | `str`, optional | Inbound `X-Request-ID` preserved verbatim when present and charset-valid (RFC 3986 unreserved characters plus common base64 characters), capped at 200 characters. Absent when missing or invalid to prevent log injection |
| `trace_id` | Structlog processor | `str`, optional | 32-character hex active trace ID. Absent outside a span |
| `span_id` | Structlog processor | `str`, optional | 16-character hex active span ID. Absent outside a span |
| `trace_flags` | Structlog processor | `str`, optional | 2-character hex active trace flags. Absent outside a span |

### Attributes on the span instead of the log record

These attributes live on the OpenTelemetry server span in your trace backend, not on log lines:

- `http.request.method`, `url.scheme`, `url.path`, `url.query`, and `server.address`
- `user_agent.original`, `client.address`, and `http.request.header.x_forwarded_for`
- `http.route` and `django.url_name`

If your Loki dashboards need to filter on any of these attributes, query them in your trace backend and join log records on `trace_id`.

### Request-ID semantics

The middleware generates a fresh per-hop `request_id` UUID4, distinct from any inbound correlation header:

- `request_id` is always a newly generated 32-character UUID4 hex string created at request entry.
- When an inbound `X-Request-ID` header is present and passes charset validation, the middleware preserves it verbatim in `upstream_request_id` (capped at 200 characters). The two fields never collide.
- Inbound values containing whitespace, control characters, or quoting characters are rejected silently, and `upstream_request_id` is omitted. Rejecting unsafe values blocks log injection and log-search hijacking.
- `X-Request-ID` is never used as `trace_id` and is never passed to the OpenTelemetry propagator. Trace context comes from `traceparent` and `X-Amzn-Trace-Id` via the composite propagator.

### Trace-header handling

The OpenTelemetry composite propagator (W3C Trace Context and AWS X-Ray, installed by `configure_otel_for_django()`) extracts inbound trace headers. **W3C `traceparent` takes precedence when both `traceparent` and `X-Amzn-Trace-Id` are present.** `X-Request-ID` is not trace context and is never fed to the propagator.

### Security

- The middleware rejects `FORBIDDEN_HEADER_NAMES` (`Authorization`, `Cookie`, `Set-Cookie`, `Proxy-Authorization`, `X-CSRFToken`, `X-CSRF-Token`, `WWW-Authenticate`, and `X-API-Key`) even when listed in `I_DOT_AI_LOGGING_HEADER_ALLOWLIST`. It emits a `structured_logging_middleware_otel_forbidden_headers_rejected` warning at startup so misconfiguration is visible.
- `I_DOT_AI_LOGGER` accepts only logger objects and zero-argument callables. It rejects dotted-import strings to prevent arbitrary boot-time code imports.
- Inbound `X-Request-ID` values undergo strict charset validation (RFC 3986 unreserved characters plus common base64 characters) before binding to `upstream_request_id`. Values with whitespace, control bytes, or quoting characters are dropped silently to block log injection and log-search hijacking.
- Request scope ownership (`REQUEST_SCOPE_OWNER_MIDDLEWARE`) prevents mid-request `refresh_context()` calls in views or helpers from clearing the request audit trail.
- Malformed settings (non-iterable `I_DOT_AI_LOGGING_EXCLUDED_PREFIXES`, invalid patterns in `I_DOT_AI_LOGGING_EXCLUDED_REGEXES`, or non-iterable `I_DOT_AI_LOGGING_HEADER_ALLOWLIST`) raise `django.core.exceptions.ImproperlyConfigured` at startup with a clear message instead of failing on the first request. Non-string items inside an otherwise valid allowlist are skipped silently.

### Notes

- The middleware is synchronous only (`sync_capable = True`, `async_capable = False`).
- Context isolation is thread-safe under Gunicorn sync and gthread workers via `structlog.contextvars`.
- `configure_otel_for_django()` is idempotent and safe to call during both `wsgi.py` and `asgi.py` initialisation.

## Bind `user.id` to log records

`StructuredLoggingMiddlewareOTel` does not extract the authenticated user because OpenTelemetry's Django instrumentation does not expose it as a span attribute and keeping user extraction separate avoids coupling the main middleware to Django's auth model.

Add the companion `DjangoUserIdMiddleware` when you need authenticated-user attribution on log records:

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "i_dot_ai_utilities.logging.middleware.django_otel.StructuredLoggingMiddlewareOTel",
    "i_dot_ai_utilities.logging.middleware.django_user_id.DjangoUserIdMiddleware",
    # Add your other middleware here.
]
```

Place `DjangoUserIdMiddleware` in this order:

1. **After `AuthenticationMiddleware`** so `request.user` exists.
2. **After `StructuredLoggingMiddlewareOTel`** so the request scope is active and `set_context_field("user.id", ...)` binds to the current request context.

### Safety guarantees

`DjangoUserIdMiddleware` preserves these safeguards:

- **No database query on an unhydrated `SimpleLazyObject`.** Django's auth middleware assigns a `SimpleLazyObject` to `request.user`, and reading `.pk` before hydration triggers a `User.objects.get(...)` query. Observability code must not issue database queries because doing so can mask database outages in the logs needed to diagnose them. The middleware checks the `_wrapped` sentinel structurally without forcing evaluation.
- **Database errors surface as warnings.** If reading `request.user.is_authenticated` or `request.user.pk` raises a Django database error (`DatabaseError`, `OperationalError`, `InterfaceError`, `ProgrammingError`, or `IntegrityError`), the middleware logs a warning naming the failing access and allows the request to continue.
- **Anonymous users omit `user.id`.** No falsy placeholder is emitted, so presence checks such as `has(user.id)` work cleanly.
- **No PII fields.** Only the opaque primary key is emitted, never email addresses, usernames, or other PII. The OpenTelemetry `enduser.*` namespace was deprecated in v1.24.

### User-ID middleware settings

`DjangoUserIdMiddleware` uses the same `I_DOT_AI_LOGGER` and `I_DOT_AI_LOGGING_MIDDLEWARE_ENABLED` settings as `StructuredLoggingMiddlewareOTel` and requires no additional configuration.

## OpenTelemetry-native logging (`i_dot_ai_utilities.otel`)

The OpenTelemetry-native path for Lambda, ECS, AWS Batch, and FastAPI, including the `i_ai` attribute naming rules, is documented in the [OpenTelemetry guide](../otel/README.md). It is separate from `StructuredLogger` and the legacy middleware above.
