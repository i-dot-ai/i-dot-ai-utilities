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

Django consumers should not need to call `refresh_context()` manually — the `StructuredLoggingMiddlewareOTel` handles it per request (see below).

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
| FastAPI / Starlette | [Request Object](https://fastapi.tiangolo.com/advanced/using-request-directly/#details-about-the-request-object) | See [FastApiRequestMetadata](./types/fastapi_enrichment_schema.py#31) |
| Lambda  | [Lambda Context Object](https://docs.aws.amazon.com/lambda/latest/dg/python-context.html) | See [LambdaContextMetadata](./types/lambda_enrichment_schema.py) |

Django consumers do **not** use a `ContextEnrichmentType` — HTTP request attributes live on OpenTelemetry spans via the Django middleware described in the next section.

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

An OpenTelemetry-backed Django middleware that automates per-request context refresh and request/response lifecycle logging. Eliminates the need for manual `refresh_context()` calls in every view.

HTTP-request attribute extraction (method / path / route / status code / user agent / client address) is delegated to `opentelemetry-instrumentation-django`, which creates a server span per request whose attributes carry those values. The middleware itself keeps only the log-lifecycle concerns that cannot live on a span: `request_started` / `request_completed` / `request_failed` events, `duration_ms`, status-driven log level, per-hop `request_id`, exclusions, header allowlist, and scope ownership.

Trace correlation on log records comes from a structlog processor reading the active span on **every** event — not just the three lifecycle events — so any `logger.info(...)` inside a view automatically gets `trace_id` / `span_id` / `trace_flags`.

**Install** (both optional extras are required):

```
pip install "i-dot-ai-utilities[django,otel]"
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

If you forget to call `configure_otel_for_django` at startup, `StructuredLoggingMiddlewareOTel` emits a loud one-shot WARNING at worker boot (`structured_logging_middleware_otel_tracer_provider_missing`) pointing straight at the fix — so the degraded "no trace ids on logs" state is visible immediately rather than showing up as silent absence.

**Wire into `settings.MIDDLEWARE`** — place after `SecurityMiddleware` and `AuthenticationMiddleware`, but before application-specific middleware so timing and logging wrap as much of the request lifecycle as possible:

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

### Emitted log events

| Event | When | Level |
|---|---|---|
| `structured_logging_middleware_otel_active` | Once per worker process at startup | `info` |
| `structured_logging_middleware_otel_forbidden_headers_rejected` | At startup iff `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` contained forbidden names that were scrubbed | `warning` |
| `structured_logging_middleware_otel_tracer_provider_missing` | At startup iff no SDK `TracerProvider` is installed (i.e. `configure_otel_for_django` was not called) | `warning` |
| `request_started` | Entering the view chain (non-excluded paths) | `info` |
| `request_completed` | View returned normally, OR raised `Http404` (404 is ordinary traffic, not a failure) | `info` (2xx/3xx), `warning` (4xx incl. `Http404`), `error` (5xx) |
| `request_failed` | View raised any unhandled exception other than `Http404` | `error` |

Exceptions are logged with full traceback via `logger.exception(...)` then re-raised with a bare `raise`, preserving the original traceback for Sentry / DRF / the debug toolbar. `Http404` is carved out per constitution Art. 46: WARNING, status 404, `request_completed` event name, no traceback (it's a control-flow signal, not a crash).

### Log record schema

The schema is deliberately narrow. HTTP request context lives on the OTel span, not the log line. Every event also carries `logging_schema_version = "1.0"` so consumers can detect breaking changes.

| Field | Source | Type | Notes |
|---|---|---|---|
| `logging_schema_version` | middleware | `str` | Always `"1.0"` in this release |
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

### What's on the span, not the log record

The following attributes live on the OTel server span and are queryable via your trace backend — NOT on the log line:

- `http.request.method`, `url.scheme`, `url.path`, `url.query`, `server.address`
- `user_agent.original`, `client.address`, `http.request.header.x_forwarded_for`
- `http.route`, `django.url_name`

If your OpenSearch / Loki dashboards need to filter on any of these, shift those queries to your trace backend and join log records on `trace_id`.

### Request-ID semantics

Constitution Art. 32 requires a fresh per-hop `request_id` UUID4, distinct from any inbound correlation value. This middleware honours that contract:

- `request_id` is **always** a freshly generated UUID4 hex, minted at request entry.
- An inbound `X-Request-ID` header, when present and charset-valid, is preserved verbatim in a separate `upstream_request_id` field (length-capped at 200 chars). The two fields never collide.
- Charset-invalid inbound values (whitespace, control characters, quoting characters) are rejected silently — the `upstream_request_id` field is simply omitted. This is security finding A3: accepting an attacker-chosen `X-Request-ID` verbatim into log context enables log-injection and log-search hijack.
- `X-Request-ID` is NEVER used as the trace id or fed to the OTel propagator. Trace context comes from `traceparent` / `X-Amzn-Trace-Id` via the composite propagator.

### Trace-header handling

OTel's composite propagator (W3C Trace Context + AWS X-Ray, installed by `configure_otel_for_django`) handles inbound headers. **W3C `traceparent` wins when both `traceparent` and `X-Amzn-Trace-Id` are present.** `X-Request-ID` is not a trace context and is never fed to the propagator - it is bound to the `upstream_request_id` log field when charset-valid.

### Security

- `FORBIDDEN_HEADER_NAMES` (Authorization, Cookie, Set-Cookie, Proxy-Authorization, X-CSRFToken, X-CSRF-Token, WWW-Authenticate, X-API-Key) are refused even when listed in `I_DOT_AI_LOGGING_HEADER_ALLOWLIST`. A `structured_logging_middleware_otel_forbidden_headers_rejected` WARNING is emitted at startup when this happens so mis-configuration is visible.
- `I_DOT_AI_LOGGER` accepts logger objects and zero-arg callables - never dotted-import strings (finding A4).
- Inbound `X-Request-ID` is charset-validated (RFC 3986 unreserved + common base64 chars) before being bound as `upstream_request_id`. Attacker-controlled values containing whitespace, control bytes, or quoting characters are dropped silently (finding A3: blocks log-injection / log-search hijack).
- Scope ownership (`REQUEST_SCOPE_OWNER_MIDDLEWARE`) guards against mid-request `refresh_context()` calls from views or helpers silently de-correlating the audit trail.
- Malformed settings (`I_DOT_AI_LOGGING_EXCLUDED_PREFIXES` non-iterable, `I_DOT_AI_LOGGING_EXCLUDED_REGEXES` with invalid patterns, `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` non-iterable) raise `django.core.exceptions.ImproperlyConfigured` at boot with a specific message — no bare `TypeError` / `re.error` / `AttributeError` crashes on the first request. Non-string entries inside an otherwise valid allowlist are dropped silently so copy-paste bugs don't brick startup.

### Notes

- Sync-only. `sync_capable = True`, `async_capable = False`.
- Thread-safe under Gunicorn sync / gthread workers via `structlog.contextvars`.
- `configure_otel_for_django` is idempotent - safe to call during both `wsgi.py` and `asgi.py` initialisation.

<br>

***

<br>

## Binding `user.id` to log records

The Django middleware deliberately does **not** extract the authenticated user. OpenTelemetry's Django instrumentation does not expose it as a span attribute, and baking the extraction into the main middleware would re-entangle this library with Django's auth model.

A thin companion middleware, `DjangoUserIdMiddleware`, does this one job safely:

```python
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "i_dot_ai_utilities.logging.middleware.django_otel.StructuredLoggingMiddlewareOTel",
    "i_dot_ai_utilities.logging.middleware.django_user_id.DjangoUserIdMiddleware",
    # ... your other middleware
]
```

Ordering matters:

1. **After `AuthenticationMiddleware`** — so `request.user` exists.
2. **After `StructuredLoggingMiddlewareOTel`** — so the request scope is already active and `set_context_field("user.id", ...)` lands on the correct per-request context.

### Safety guarantees

The middleware lifts the hardening that used to live inside the deleted `DjangoEnricher`:

- **No database query from an unhydrated `SimpleLazyObject`** (security finding FI-5). Django's auth middleware assigns a `SimpleLazyObject` to `request.user`; touching `.pk` before it hydrates triggers a `User.objects.get(...)` query. Observability code must never issue such a query — it masks database outages from the very logs that would diagnose them. The middleware detects the unhydrated state structurally (`_wrapped` sentinel) without forcing evaluation.
- **Database errors surface as WARNINGs**, not silent drops. If reading `request.user.is_authenticated` or `request.user.pk` raises a Django `DatabaseError` / `OperationalError` / `InterfaceError` / `ProgrammingError` / `IntegrityError`, the middleware emits a WARNING naming the failing access and continues; the request is unaffected but operators see the outage.
- Anonymous users produce no `user.id` field at all — not a falsy value — so `has(user.id)` queries discriminate cleanly.
- The OTel `enduser.*` namespace was deprecated in v1.24; only the opaque primary key is emitted, never email / username / other PII.

### Settings

`DjangoUserIdMiddleware` honours the same `I_DOT_AI_LOGGER` and `I_DOT_AI_LOGGING_MIDDLEWARE_ENABLED` settings as `StructuredLoggingMiddlewareOTel`; no additional configuration is required.
