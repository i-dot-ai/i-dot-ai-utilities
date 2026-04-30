# Structured Logging

## Usage

### Create a Logger

You can create the most basic version of the logger by simply instantiating a new object of the logger class.
```python
logger = StructuredLogger()

...
logger.info(...)
```
This is enough to format your logs for consumption by downstream log subscribers.

<br>

***

<br>

A more productionised version might look something like this - this uses console-based logging when running locally, and structures logging into JSON/enriches context when running in ECS (Fargate).
```python
environment = os.environ.get('ENVIRONMENT')
logger_environment = ExecutionEnvironmentType.LOCAL if environment == "LOCAL" else ExecutionEnvironmentType.FARGATE
logger_format = LogOutputFormat.TEXT if environment == "LOCAL" else LogOutputFormat.JSON

logger = StructuredLogger(level='info', options={
    "execution_environment": logger_environment,
    "log_format": logger_format,
})
```

<br>

***

<br>

### Creating Log Messages
Once the logger is initialised, you can create log messages in different ways depending on your requirement.
For example, you can create simple messages with string-literals:

```python
logger.info("A thing happened")
```

Or you might add some context fields to the log message so they become available in the downstream logging stack:
```python
logger.info("Anothing thing happened", thing_id=12345, user_logged_in=True)
```

You can also format strings so the message field itself contains useful data, whilst also capturing that useful data as separate fields:
```python
logger.info("Yet another thing occurred for user {email} with id {id}", email=user_email, id=id)
```

It is best practice to NOT to use f-strings in log message creation. Variable interpolation happens within the function itself and allows the library to extract message content correctly so it can be indexed downstream.

<br>

***

<br>

Exceptions are added to the message output automatically when called inside of an `except` block:
```python
logger.exception("Something went wrong when user {email} logging in", email=email)
```

This will log the message, and inject the exception into the log context automatically.

<br>

***

<br>

### Refreshing the Logger

You should always refresh the logger context at the entrypoints of your API/application. This generates a new context ID and resets the logger for that invocation by removing any custom fields that were previously added by enrichers (see examples below) or through `set_context_field()`.
```python
@app.post("/do/the/thing")
async def do_the_thing(request: Request):

    logger.refresh_context()

    do_stuff()
    ...
```

<br>

***

<br>

### Setting Execution Environment Type and Log Format

#### Environment Type

Environment Type is configurable using the `execution_environment` option setting when instantiating the logger. Accepted values are defined in the `ExecutionEnvironmentType` enum (see [here](./types/enrichment_types.py)).

The logger will automatically extract important information from the execution environment and enrich log messages using it. 

#### Log Format

Log format is configurable using the `log_format` option setting when instantiating the logger. Accepted values are defined in the `LogOutputFormat` enum (see [here](./types/log_output_format.py)).

The ability to change log formats exists to give developers a friendlier log output when developing locally. They should be set to JSON format when running on the platform so structured logs can be read and processed in our logging stack downstream.

<br>

***

<br>

### Implementing Persistent Context
Context can be automatically be added to log messages by using enrichers. Enrichers are helpers provided to you to automatically extract context related to a given execution:
```python
@app.get("/")
async def root(request: Request):

    logger.refresh_context(context_enrichers=[
        {
            "type": ContextEnrichmentType.FASTAPI,
            "object": request,
        }
    ])

    do_stuff()
```
The above example would extract information from the FastAPI request object (query string, path, user agent, etc) and inject it into all subsequent log messages until `refresh_context()` is called again.

Each Context Enricher object accepts a `type` and `object`. `type` is a `ContextEnrichmentType` enum ([see here](./types/enrichment_types.py) for accepted values). `object` is the Input Object to pass into the logger for context extraction - see the table below for further details.

The following context enrichers are available for use:
| Name | Input Object | Extracted Fields |
|---------|-------------------------------------------------------------------------------------------------------|----------------------------------------------------------------|
| FastAPI / Starlette | [Request Object](httpsd:'\[]:?PO-//fastapi.tiangolo.com/advanced/using-request-directly/#details-about-the-request-object) | See [FastApiRequestMetadata](./types/fastapi_enrichment_schema.py#31) |
| Lambda  | [Lambda Context Object](https://docs.aws.amazon.com/lambda/latest/dg/python-context.html) | See [LambdaContextMetadata](./types/lambda_enrichment_schema.py) |
| Django  | [Django HttpRequest](https://docs.djangoproject.com/en/stable/ref/request-response/#httprequest-objects) | See [DjangoRequestMetadata](./types/django_enrichment_schema.py) |


<br>

***

<br>

### Django usage example

Django applications should call `refresh_context()` at the start of each view (or from a middleware) with a `DJANGO` enricher. The enricher mirrors the FastAPI enricher's `request.*` shape and adds a `django.*` block containing routing metadata.

```python
from django.conf import settings
from i_dot_ai_utilities.logging.types.enrichment_types import ContextEnrichmentType

logger = settings.LOGGER


def my_view(request):
    logger.refresh_context(context_enrichers=[
        {
            "type": ContextEnrichmentType.DJANGO,
            "object": request,
        }
    ])

    logger.info("handling request")
    ...
```

The enricher never imports Django: any object that quacks like an `HttpRequest` (i.e. exposes `method`, `path`, `scheme`, `META`, `GET`, `headers`, `get_host()`) will work. `resolver_match` and `user` are accessed defensively so pre-routing, 404, and pre-auth-middleware cases are handled gracefully.

#### Fields emitted

The Django enricher emits a flat dict of [OpenTelemetry semantic-convention](https://opentelemetry.io/docs/specs/semconv/http/http-spans/) field names, plus a small `django.*` namespace for framework-specific values that have no OTel equivalent. Optional fields are **omitted** from the log line when their source is absent (not emitted as `None` / `""`).

| Field | Always emitted? | Notes |
|-------|-----------------|-------|
| `http.request.method` | Yes | From `request.method` |
| `url.scheme` | Yes | `http` or `https` |
| `url.path` | Yes | From `request.path` |
| `url.query` | Yes | May be an empty string |
| `server.address` | Yes | From `request.get_host()` |
| `user_agent.original` | When header present | From `User-Agent` |
| `client.address` | When `REMOTE_ADDR` present | Peer Django sees. Behind a load balancer this is the proxy, not the true client |
| `http.request.header.x_forwarded_for` | When header present | Emitted separately so operators can trust it only if a known proxy is in front |
| `http.route` | When resolver has matched | From `request.resolver_match.view_name` (`None` before routing / on 404) |
| `django.url_name` | When resolver has matched | From `request.resolver_match.url_name`. Django-specific; no OTel equivalent |
| `user.id` | When auth middleware has run AND `request.user.is_authenticated is True` | `str(request.user.pk)`, falls back to `request.user.id` only when `pk is None`. Anonymous users produce no `user.id` field — not a falsy value — so `has(user.id)` queries are clean |

#### Security warning

`url.query` is logged as the **raw** encoded query string. Do not pass secrets, tokens, session IDs, or PII via query parameters in services using this enricher - they will be written to logs and shipped downstream verbatim. Prefer headers or request bodies for sensitive values.

#### Migration note for non-Django consumers

Upgrading to the version of `i-dot-ai-utilities` that introduces the Django enricher is a **drop-in change** for FastAPI-only and Lambda-only consumers:

- No new runtime dependency on Django (the enricher duck-types `HttpRequest` via a Protocol).
- No new required optional extra in `pyproject.toml`.
- `ContextEnrichmentType.FASTAPI` and `ContextEnrichmentType.LAMBDA` keep their existing enum values.
- The `ContextEnrichmentOptions.object` union is widened only; existing call-sites need no changes.
- FastAPI and Lambda extraction behaviour is unchanged.

No action is required for FastAPI-only consumers other than updating the pinned version.


<br>

***

<br>

### Adding your own context fields

You can add custom fields to your logger, which will appear on each log message going forward until `refresh_context()` is called again. This is useful for enriching your own context onto log messages once important information has been discovered during execution.
```python
@app.post("/login")
async def login(request: Request):
    logger.refresh_context()

    user_email = get_user_info(request)

    logger.info("User {email} started login process", email=user_email)
    logger.set_context_field("email", user_email)

    ...
```
Fields added in this manner will be indexed and searchable in the downstream logging stack.

<br>

***

<br>

## Django Structured Logging Middleware

A drop-in Django middleware that automates per-request context refresh, trace-header correlation, and request/response lifecycle logging. Eliminates the need for manual `refresh_context()` calls in every view.

**Install** (Django is an optional extra):

```
pip install "i-dot-ai-utilities[django]"
```

**Wire into Django `MIDDLEWARE`** — place after `SecurityMiddleware` and `AuthenticationMiddleware`, but before application-specific middleware so timing and logging wrap as much of the request lifecycle as possible:

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "i_dot_ai_utilities.logging.middleware.django.StructuredLoggingMiddleware",
    # ... your other middleware
]
```

Position guidance is documented, not enforced. You are free to reorder; the middleware will simply reflect whatever ordering you choose.

### Settings

All settings are optional. The middleware ships with sensible defaults and uses `getattr(settings, ...)` with fallbacks, so you can add them only when you want to override behaviour.

| Setting | Type | Default | Purpose |
|---|---|---|---|
| `I_DOT_AI_LOGGER` | logger object \| zero-arg callable | Bare `structlog.get_logger(__name__)` wrapped for the five-method contract | The logger the middleware writes through. **Dotted import strings are NOT accepted** (security finding A4 — closes a boot-time arbitrary-import attack surface). Import your logger in `settings.py` and assign it directly, or pass a zero-arg factory. |
| `I_DOT_AI_LOGGING_MIDDLEWARE_ENABLED` | `bool` | `True` | Set to `False` to disable. The middleware then raises `MiddlewareNotUsed` cleanly at startup. |
| `I_DOT_AI_LOGGING_EXCLUDED_PREFIXES` | iterable of `str` | Health-check prefixes (see below) | Paths whose prefix matches are skipped entirely — no log events. |
| `I_DOT_AI_LOGGING_EXCLUDED_REGEXES` | iterable of `str` \| `re.Pattern` | `()` | Additional regex-based exclusions, compiled once at startup. |
| `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` | iterable of `str` | `()` | Header names to bind to the log context (truncated to 512 chars). Explicit allowlist only — never a denylist. A hard-coded denylist (`Authorization`, `Cookie`, `Set-Cookie`, `Proxy-Authorization`, `X-CSRFToken`, `WWW-Authenticate`, `X-API-Key`) is always applied on top; names on the denylist are silently filtered out of the allowlist and a warning is emitted at startup. |

**Default excluded prefixes** (skipping all high-volume health probes):

- `/health/`
- `/healthz`
- `/api/health/`
- `/api/health/live`
- `/api/health/ready`

### Example configuration

```python
# settings.py
from myapp.logging import build_logger  # imported in your own code — no dotted string

I_DOT_AI_LOGGER = build_logger()  # or: = build_logger  (zero-arg callable)
I_DOT_AI_LOGGING_EXCLUDED_PREFIXES = (
    "/healthz",
    "/api/internal/",
)
I_DOT_AI_LOGGING_HEADER_ALLOWLIST = ("X-Tenant-ID",)
```

### Emitted events

| Event | When | Level |
|---|---|---|
| `structured_logging_middleware_active` | Once per worker process at startup | `info` |
| `request_started` | Entering the view chain (non-excluded paths) | `info` |
| `request_completed` | View returned normally | `info` (2xx/3xx), `warning` (4xx), `error` (5xx) |
| `request_failed` | View raised an unhandled exception | `error` |

Exceptions are logged with full traceback via `logger.exception(...)` then re-raised with a bare `raise`, preserving the original traceback for Sentry / DRF / the debug toolbar.

### Log schema (version 1.0)

The full authoritative schema lives in
[`middleware/SCHEMA.md`](./middleware/SCHEMA.md). The table below summarises
the fields emitted on `request_*` events; every event also carries
`logging_schema_version = "1.0"` so consumers can detect breaking changes.

The middleware and the Django enricher together emit OpenTelemetry HTTP server semantic-convention field names where applicable, so downstream observability tooling (Grafana, Honeycomb, CloudWatch Logs Insights, Datadog) can auto-correlate.

The enricher (invoked via `refresh_context` at the top of every request) is responsible for fields that are a pure function of the request object. The middleware is responsible for fields that only exist once the HTTP lifecycle has progressed (status, duration, exception, trace propagation).

| Field | Source | Type | Notes |
|---|---|---|---|
| `logging_schema_version` | middleware | `str` | Always `"1.0"` in this release |
| `http.request.method` | enricher | `str` | Method from `request.method` |
| `url.scheme` | enricher | `str` | `http` or `https` |
| `url.path` | enricher | `str` | From `request.path`, capped at 2048 chars |
| `url.query` | enricher | `str` | Raw query string, may be empty, capped at 1024 chars |
| `server.address` | enricher | `str` | From `request.get_host()` |
| `user_agent.original` | enricher | `str`, optional | From `User-Agent` header, capped at 512 chars |
| `client.address` | enricher | `str`, optional | From `REMOTE_ADDR`. Does NOT trust `X-Forwarded-For` |
| `http.request.header.x_forwarded_for` | enricher | `str`, optional | Raw `X-Forwarded-For` header, capped at 512 chars |
| `http.route` | enricher + middleware | `str`, optional | Matched view name. Enricher emits pre-routing value (if any); middleware re-binds after the resolver has run |
| `django.url_name` | enricher + middleware | `str`, optional | Django URL name. Same re-bind behaviour as `http.route` |
| `user.id` | enricher | `str`, optional | `str(request.user.pk)` only when auth middleware has run AND `is_authenticated is True`. Renamed from `enduser.id` (OTel deprecated the namespace in v1.24) |
| `http.response.status_code` | middleware | `int` | Captured after the view returns; synthesised to `500` on exception, `404` on `Http404` |
| `error.type` | middleware | `str`, optional | Status code string (e.g. `"500"`) on 4xx/5xx responses; fully-qualified exception class name (e.g. `"myapp.errors.PaymentError"`) on the exception path. Per OTel HTTP semconv |
| `http.request.header.*` | middleware | `str` | Only for headers explicitly in `I_DOT_AI_LOGGING_HEADER_ALLOWLIST`; lowercased, hyphens to underscores |
| `duration_ms` | middleware | `int` | `time.monotonic()` delta, clamped to ≥ 0 |
| `trace_id` | middleware | `str` | Resolved via precedence ladder (see below) |
| `trace_id_source` | middleware | `str` | One of `traceparent` / `amzn` / `request_id` / `synthetic` |
| `span_id` | middleware | `str`, optional | Only set when W3C `traceparent` parsed successfully (lowercase 16-hex) |
| `trace_flags` | middleware | `str`, optional | 2-hex byte with only defined bits preserved |
| `request_id` | middleware | `str` | Always a fresh per-hop UUID4 hex (32 chars) |
| `upstream_request_id` | middleware | `str`, optional | Verbatim inbound `X-Request-ID` (length-capped) |
| `amzn_trace_root` | middleware | `str`, optional | Verbatim `Root=` from `X-Amzn-Trace-Id` (preserves the `1-` version prefix) |
| `exception.type` | middleware | `str` | On `request_failed` OR `Http404`-induced `request_completed`; `type(exc).__name__` |

Schema version: **1.0**.

### Trace-header precedence

The `trace_id` field resolves from inbound headers in this fixed order, stopping at the first match:

1. `traceparent` — parsed per [W3C Trace Context Level 1](https://www.w3.org/TR/trace-context/). Lowercase-hex only; all-zero ids, version `ff`, and malformed inputs are silently ignored.
2. `X-Amzn-Trace-Id` — `Root=` segment including the `1-` version prefix. `Self=` is intentionally NOT used as the trace id.
3. `X-Request-ID` — verbatim opaque string, never UUID-validated, never regenerated when present.
4. Freshly generated UUID4 hex.

A per-hop `request_id` UUID4 is ALWAYS bound separately from `trace_id`, so you always have both distributed-trace correlation and per-hop identity.

### Security

The middleware NEVER logs:

- `Authorization`, `Cookie`, `Set-Cookie`, `Proxy-Authorization`
- CSRF tokens, session identifiers
- Request bodies, response bodies

Headers outside `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` are not captured. `X-Forwarded-For` is included in its own explicit field when present but is never used as `client.address` (too easily forged without a known proxy). All captured strings are length-capped.

**Query-string PII warning.** `url.query` is logged as the **raw** encoded query string — the enricher (which produces this field) does not redact it. Do not pass secrets, tokens, session IDs, or PII via query parameters in services using this middleware; they will be written to logs and shipped downstream verbatim. Prefer headers or request bodies for sensitive values. If you must accept sensitive data via query parameters, strip or redact before the request reaches this middleware.

### Notes

- Sync-only. `sync_capable = True`, `async_capable = False`. Async Django consumers will incur `async_to_sync` overhead.
- Thread-safe under Gunicorn sync / gthread workers via `structlog.contextvars`. Consumers must have `structlog.contextvars.merge_contextvars` as the first processor in their structlog pipeline (the library's `StructuredLogger` configures this automatically).
- `refresh_context()` runs as the very first statement in `__call__`, so request context cannot leak between sequential requests on the same worker thread.


<br>

***

<br>

## Django Structured Logging Middleware (OTel edition)

An OpenTelemetry-backed alternative to `StructuredLoggingMiddleware`. Ships alongside the existing middleware - **both can be installed at the same time**, and you pick which one to use via `settings.MIDDLEWARE`. Intended for services that are converging on OTel as the unified observability stack.

Where the existing middleware both *extracts* HTTP context (via `DjangoEnricher`) and *emits* it on every log line, the OTel variant delegates HTTP-context extraction to `opentelemetry-instrumentation-django`. The instrumentor creates a server span per request whose attributes carry `http.request.method`, `url.path`, `http.route`, `http.response.status_code`, and so on. The middleware itself keeps only the log-lifecycle concerns that cannot live on a span: `request_started` / `request_completed` / `request_failed` events, `duration_ms`, status-driven log level, per-hop `request_id`, exclusions, header allowlist, and scope ownership.

Trace correlation on log records comes from a structlog processor reading the active span on **every** event - not just the three lifecycle events - so any `logger.info(...)` inside a view automatically gets `trace_id` / `span_id` / `trace_flags`.

**Install** (optional extra):

```
pip install "i-dot-ai-utilities[otel]"
```

**Configure OTel once at startup** (`wsgi.py`, `asgi.py`, or an `AppConfig.ready()`):

```python
from i_dot_ai_utilities.logging._otel import configure_otel_for_django

configure_otel_for_django(service_name="my-service")
```

By default spans are exported to stdout via `ConsoleSpanExporter` - useful during local development. Pass a real exporter for production:

```python
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

configure_otel_for_django(
    service_name="my-service",
    span_exporter=OTLPSpanExporter(endpoint="https://otel-collector.example/v1/traces"),
)
```

**Wire into `settings.MIDDLEWARE`** (same positioning guidance as the original middleware: after `SecurityMiddleware` and `AuthenticationMiddleware`):

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "i_dot_ai_utilities.logging.middleware.django_otel.StructuredLoggingMiddlewareOTel",
    # ... your other middleware
]
```

`DjangoInstrumentor` inserts its own middleware at the front of the chain automatically; do not add it manually.

### Wiring the trace-context structlog processor

`configure_otel_for_django` can insert the processor into your structlog chain for you:

```python
import structlog
from i_dot_ai_utilities.logging._otel import configure_otel_for_django

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

Or wire it yourself using the standalone helper:

```python
from i_dot_ai_utilities.logging._otel import otel_trace_context_processor

processors = [
    structlog.contextvars.merge_contextvars,
    structlog.processors.add_log_level,
    otel_trace_context_processor,          # <- before the renderer
    structlog.processors.JSONRenderer(),
]
```

### Settings

Same setting names as the original middleware. All optional.

| Setting | Type | Default | Purpose |
|---|---|---|---|
| `I_DOT_AI_LOGGER` | logger object \| zero-arg callable | Fresh structlog bound logger | The logger the middleware writes through |
| `I_DOT_AI_LOGGING_MIDDLEWARE_ENABLED` | `bool` | `True` | Set to `False` to disable cleanly via `MiddlewareNotUsed` |
| `I_DOT_AI_LOGGING_EXCLUDED_PREFIXES` | iterable of `str` | Health-check prefixes | Paths whose prefix matches are skipped entirely |
| `I_DOT_AI_LOGGING_EXCLUDED_REGEXES` | iterable of `str` \| `re.Pattern` | `()` | Regex-based exclusions, compiled once at startup |
| `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` | iterable of `str` | `()` | Header names to bind to the log context (truncated to 512 chars). Forbidden headers in `FORBIDDEN_HEADER_NAMES` are refused even if listed here. |

### Emitted log events

Same three lifecycle events as the original middleware, with a distinct startup marker so operators can tell the two apart:

| Event | When | Level |
|---|---|---|
| `structured_logging_middleware_otel_active` | Once per worker process at startup | `info` |
| `structured_logging_middleware_otel_forbidden_headers_rejected` | At startup iff `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` contained forbidden names that were scrubbed | `warning` |
| `request_started` | Entering the view chain (non-excluded paths) | `info` |
| `request_completed` | View returned normally, OR raised `Http404` (404 is ordinary traffic, not a failure) | `info` (2xx/3xx), `warning` (4xx incl. `Http404`), `error` (5xx) |
| `request_failed` | View raised any unhandled exception other than `Http404` | `error` |

Exceptions are logged with full traceback via `logger.exception(...)` then re-raised with a bare `raise`, preserving the original traceback for Sentry / DRF / the debug toolbar. `Http404` is carved out per constitution Art. 46: WARNING, status 404, `request_completed` event name, no traceback (it's a control-flow signal, not a crash).

### Log record schema

Schema is **deliberately narrower** than the original middleware. HTTP request context is on the OTel span, not the log line. Every event also carries `logging_schema_version = "1.0"` so consumers can detect breaking changes.

| Field | Source | Type | Notes |
|---|---|---|---|
| `logging_schema_version` | middleware | `str` | Always `"1.0"` in this release. Distinct from the original middleware's schema version |
| `http.response.status_code` | middleware | `int` | Captured after the view returns. Synthesised to `500` on unhandled exception, `404` on `Http404` |
| `error.type` | middleware | `str`, optional | Status code string (e.g. `"404"`, `"500"`) on 4xx/5xx responses and `Http404`. Fully-qualified exception class name (e.g. `"builtins.RuntimeError"`) on the unhandled-exception path. Absent on 2xx/3xx. Per OTel HTTP semconv note 4 |
| `exception.type` | middleware | `str`, optional | `type(exc).__name__` on `request_failed` and on the `Http404`-induced `request_completed` |
| `http.request.header.*` | middleware | `str` | Only for headers explicitly in `I_DOT_AI_LOGGING_HEADER_ALLOWLIST`; lowercased, hyphens to underscores. Length-capped at 512 chars |
| `duration_ms` | middleware | `int` | `time.monotonic()` delta, clamped to >= 0 |
| `request_id` | middleware | `str` | **Always** a fresh per-hop UUID4 hex (32 chars). Distinct from `trace_id` and from any inbound correlation id |
| `upstream_request_id` | middleware | `str`, optional | Inbound `X-Request-ID` preserved verbatim when present and charset-valid (RFC 3986 unreserved + common base64 chars). Length-capped at 200 chars. Absent when no inbound header or when the inbound value fails charset validation (security finding A3: blocks log-injection via attacker-chosen identifiers) |
| `trace_id` | structlog processor | `str`, optional | 32-hex active trace id; absent outside a span |
| `span_id` | structlog processor | `str`, optional | 16-hex active span id; absent outside a span |
| `trace_flags` | structlog processor | `str`, optional | 2-hex active trace flags; absent outside a span |

### What's gone (vs the original middleware)

These fields **no longer appear on log records** when using `StructuredLoggingMiddlewareOTel`. They live on the OTel span attributes and are queryable via your trace backend:

- `http.request.method`, `url.scheme`, `url.path`, `url.query`, `server.address`
- `user_agent.original`, `client.address`, `http.request.header.x_forwarded_for`
- `http.route`, `django.url_name`
- `user.id` - **dropped entirely**. Consumers who need user attribution on log records must bind it explicitly from a view or a separate thin middleware (e.g. `logger.set_context_field("user.id", str(request.user.pk))` after authentication).
- `amzn_trace_root` - trace correlation comes from the OTel span context, not from verbatim header copies.
- `trace_id_source` - provenance of the trace id is conveyed implicitly by whether a span context is active. Absence of `trace_id` on a log line means "no span was active", which replaces the original middleware's `trace_id_source="synthetic"` marker.

OpenSearch dashboards that filter on any of the above will need updating. The cleanest migration is to shift those queries to your trace backend, joining log records on `trace_id`.

### Request-ID semantics

Constitution Art. 32 requires a fresh per-hop `request_id` UUID4, distinct from any inbound correlation value. This middleware honours that contract:

- `request_id` is **always** a freshly generated UUID4 hex, minted at request entry.
- An inbound `X-Request-ID` header, when present and charset-valid, is preserved verbatim in a separate `upstream_request_id` field (length-capped at 200 chars). The two fields never collide.
- Charset-invalid inbound values (whitespace, control characters, quoting characters) are rejected silently — the `upstream_request_id` field is simply omitted. This is security finding A3: accepting an attacker-chosen `X-Request-ID` verbatim into log context enables log-injection and log-search hijack.
- `X-Request-ID` is NEVER used as the trace id or fed to the OTel propagator. Trace context comes from `traceparent` / `X-Amzn-Trace-Id` via the composite propagator.

### Trace-header handling

OTel's composite propagator (W3C Trace Context + AWS X-Ray, installed by `configure_otel_for_django`) handles inbound headers. Precedence matches the original middleware: **W3C `traceparent` wins when both `traceparent` and `X-Amzn-Trace-Id` are present**. `X-Request-ID` is not a trace context and is never fed to the propagator - it continues to be bound to the `request_id` log field (verbatim if inbound, fresh UUID4 otherwise).

### Security

Same security stance as the original middleware:

- `FORBIDDEN_HEADER_NAMES` (Authorization, Cookie, Set-Cookie, Proxy-Authorization, X-CSRFToken, X-CSRF-Token, WWW-Authenticate, X-API-Key) are refused even when listed in `I_DOT_AI_LOGGING_HEADER_ALLOWLIST`. A `structured_logging_middleware_otel_forbidden_headers_rejected` WARNING is emitted at startup when this happens so mis-configuration is visible.
- `I_DOT_AI_LOGGER` accepts logger objects and zero-arg callables - never dotted-import strings (finding A4).
- Inbound `X-Request-ID` is charset-validated (RFC 3986 unreserved + common base64 chars) before being bound as `upstream_request_id`. Attacker-controlled values containing whitespace, control bytes, or quoting characters are dropped silently (finding A3: blocks log-injection / log-search hijack).
- Scope ownership (`REQUEST_SCOPE_OWNER_MIDDLEWARE`) guards against mid-request `refresh_context()` calls from views or helpers silently de-correlating the audit trail.
- Malformed settings (`I_DOT_AI_LOGGING_EXCLUDED_PREFIXES` non-iterable, `I_DOT_AI_LOGGING_EXCLUDED_REGEXES` with invalid patterns, `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` non-iterable) raise `django.core.exceptions.ImproperlyConfigured` at boot with a specific message — no bare `TypeError` / `re.error` / `AttributeError` crashes on the first request. Non-string entries inside an otherwise valid allowlist are dropped silently so copy-paste bugs don't brick startup.
- The hardening specific to the `DjangoEnricher` (A1 `isinstance` check, FI-5 database-error handling, SimpleLazyObject user detection) is **not present** in the OTel variant - because the enricher is not called at all. OTel's own Django instrumentation handles the HTTP request object; it does not extract authenticated user.

### Running both middlewares in parallel

Both middlewares ship in the same package. They share setting names (`I_DOT_AI_LOGGER`, exclusions, header allowlist, enable flag) but can be activated independently via `settings.MIDDLEWARE`. Typical migration path:

1. Install the `[otel]` extra and call `configure_otel_for_django` at startup, but keep `StructuredLoggingMiddleware` in `settings.MIDDLEWARE`. Spans start flowing; log shape is unchanged.
2. Verify spans in your trace backend and trace-log correlation works end to end.
3. Swap `StructuredLoggingMiddleware` for `StructuredLoggingMiddlewareOTel` in `settings.MIDDLEWARE`. Log shape changes - see the "What's gone" section - and OpenSearch dashboards filtering on HTTP fields need updating.
4. Once stable, the `[django]` extra (and the `DjangoEnricher` it enables) becomes optional.

Rolling back is a one-line `settings.MIDDLEWARE` change; no data migration required.

### Notes

- Sync-only. `sync_capable = True`, `async_capable = False`.
- Thread-safe under Gunicorn sync / gthread workers via `structlog.contextvars`.
- `configure_otel_for_django` is idempotent - safe to call during both `wsgi.py` and `asgi.py` initialisation.
