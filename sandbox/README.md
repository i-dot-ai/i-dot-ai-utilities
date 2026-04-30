# Logging sandbox

A self-contained localhost stack for exercising the `i-dot-ai-utilities` structured logger
(`i-dot-ai-utilities/src/i_dot_ai_utilities/logging/`) end-to-end against an OTel-compliant
observability backend.

## What's in the box

| Service       | Port   | Role                                                                         |
| ------------- | ------ | ---------------------------------------------------------------------------- |
| `fastapi-app` | 8001   | FastAPI demo, uses `ContextEnrichmentType.FASTAPI`                           |
| `django-app`  | 8003   | Django demo, uses `StructuredLoggingMiddlewareOTel` + `DjangoUserIdMiddleware` |
| `otelcol`     | –      | OTel Collector Contrib — tails docker json-file logs, OTLP-exports           |
| `lgtm`        | 3000   | `grafana/otel-lgtm` all-in-one: Loki + Tempo + Mimir + Grafana               |
| `k6`          | –      | Weighted traffic generator against both apps                                 |

Tempo lights up with real spans for `django-app` (Django
auto-instrumentation is configured via `configure_otel_for_django`). Mimir
stays idle — no metrics are produced.

## Prereqs

- Docker Desktop or Colima for Mac with at least 4 GB allocated to the VM.
- First run pulls ~1.8 GB (LGTM image + OTel Python wheels).

## Running

```sh
cd i-dot-ai-utilities/sandbox
docker compose up --build     # or `docker-compose` if the plugin isn't installed
```

Then:

- Grafana:  <http://localhost:3000>  (anonymous admin, no login)
- FastAPI:  <http://localhost:8001>
- Django:   <http://localhost:8003>

Tear down:

```sh
docker compose down -v
```

## Poking around Grafana

1. Open <http://localhost:3000>.
2. Click the compass icon (Explore) in the left nav.
3. Pick the **Loki** data source.
4. Try these LogQL queries:

```logql
# All logs from every demo app:
{service_name=~"fastapi-demo|django-demo"}

# Parse the structlog JSON so individual fields become filterable:
{service_name="fastapi-demo"} | json

# Only exception / error logs:
{service_name=~".+-demo"} | json | level="error"

# All 500 responses:
{service_name="django-demo"} |= "request_completed" | json | http_response_status_code="500"

# Slow requests:
{service_name="django-demo"} | json | duration_ms > 200

# Correlate a single trace across apps (paste a trace_id from the sidebar):
{service_name=~".+-demo"} | json | trace_id="<paste-a-trace_id>"
```

The `service_name` label is derived from the logger's `logger_name` option
(`fastapi-demo` / `django-demo`). Business-level fields set via
`set_context_field()` (e.g. `user_id`) and the OTel trace processor's
`trace_id` / `span_id` / `trace_flags` arrive as Loki indexed labels
after `| json`.

## What you'll see in the Django log stream

The OTel middleware deliberately keeps log records narrow. HTTP request
context (method, path, route, status, user agent, client address) lives
on the server span; joining logs to spans by `trace_id` in Tempo gives
the full picture.

| Field on log record                                    | Emitted on django-app |
| ------------------------------------------------------ | :-------------------: |
| `trace_id` / `span_id` / `trace_flags`                 | yes (from active span) |
| `request_id`                                           | yes                   |
| `upstream_request_id` (inbound X-Request-ID verbatim)  | yes when present      |
| `duration_ms`                                          | yes                   |
| `http.response.status_code`                            | yes                   |
| `exception.type` / `error.type` (on failures)          | yes                   |
| `http.request.header.*` (allowlisted only)             | yes                   |
| `http.request.method` / `url.path` / `url.query`       | **no** — on the span  |
| `server.address` / `user_agent.original`               | **no** — on the span  |
| `client.address` / `http.request.header.x_forwarded_for` | **no** — on the span |
| `http.route` / `django.url_name`                       | **no** — on the span  |
| `user.id`                                              | when `DjangoUserIdMiddleware` wired and auth-middleware populated `request.user` |

## Tempo: traces for `django-app`

Because `django-app` runs `configure_otel_for_django(...)` at startup,
every request produces a Django server span exported via OTLP to the LGTM
image's internal Tempo.

In Grafana Explore → Tempo datasource:

1. **Search** → Service Name = `django-demo` → hit Run.
2. Click any span. You'll see `http.request.method` / `url.path` /
   `http.route` / `http.status_code` as *span attributes* — the very fields
   the middleware deliberately stops emitting on the log record.
3. Copy the `trace_id` from the span details and run
   `{service_name="django-demo"} | json | trace_id="<paste>"` in Loki
   to see every log line that was emitted inside that span — the
   `otel_trace_context_processor` binds the active span to each event, so
   you get trace↔log correlation for free.

## Iterating on the utility

The utility source (`../src/i_dot_ai_utilities/...`) is bind-mounted read-only
into every app container at `/opt/utility` and installed in editable mode at
container start. To pick up a code change:

```sh
docker compose restart fastapi-app django-app
```

No image rebuild needed.

## What each endpoint exercises

Both apps expose the same endpoints (Django paths have trailing slashes):

| Endpoint            | Logger paths exercised                                              |
| ------------------- | ------------------------------------------------------------------- |
| `GET /`             | Plain `logger.info` + context enrichment                            |
| `GET /users/{id}`   | Template interpolation (`"user {id}"`) + `set_context_field`        |
| `GET /users/-1`     | Warning path (k6 never hits this; try it manually)                  |
| `GET /search?q=...` | `url.query` — on the span for Django, on the log for FastAPI        |
| `GET /slow`         | `logger.warning` + `duration_ms` visible via middleware             |
| `GET /boom`         | `logger.exception` inside an `except` block                         |
| `GET /health`       | Django: excluded by middleware default prefixes (no logs emitted)   |

The k6 script injects a random W3C `traceparent` header on ~50 % of requests
so you can observe trace propagation (`DjangoInstrumentor`'s composite
propagator extracts and continues the trace). It also injects `X-Tenant-ID`
on some requests to prove `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` is being
honoured.

## Running / re-running the load generator

The k6 service runs automatically as part of `docker compose up` — the
default is 5 minutes, 5 VUs, then the container exits cleanly.

To re-run after it has exited, or to kick off a fresh run at any time:

```sh
# Default: 5m duration, 5 VUs, both apps.
docker compose run --rm k6 run /scripts/script.js

# Shorter smoke run:
docker compose run --rm -e K6_DURATION=30s -e K6_VUS=2 k6 run /scripts/script.js

# Long burn for capacity testing:
docker compose run --rm -e K6_DURATION=30m -e K6_VUS=20 k6 run /scripts/script.js

# Only hit a single app (point the other at an unreachable URL so k6
# skips it - the script picks a target uniformly at random):
docker compose run --rm \
    -e FASTAPI_URL=http://localhost:1 \
    k6 run /scripts/script.js

# Follow the live run:
docker compose logs -f k6
```

You can also run k6 directly on the host against the exposed ports (handy
for quick iteration on the script itself):

```sh
brew install k6
FASTAPI_URL=http://localhost:8001 \
DJANGO_URL=http://localhost:8003 \
K6_DURATION=30s \
k6 run i-dot-ai-utilities/sandbox/loadgen/script.js
```

All knobs honoured: `K6_DURATION`, `K6_VUS`, `FASTAPI_URL`, `DJANGO_URL`.

## Known gaps / gotchas

- **First LGTM startup is slow.** Give it ~30 s after `docker compose up`
  before logs begin flowing in Grafana.
- **Log volumes on Docker Desktop / Colima for Mac** live inside the VM. The
  collector bind-mounts `/var/lib/docker/containers` from that VM, which is
  the standard pattern and works. The container log dir is root-owned mode
  0710 on Linux hosts, so the collector runs as `user: "0:0"` (root) to read
  it.
- **The `StructuredLogger` overwrites structlog's global processor chain in
  its `__init__`.** That means `configure_otel_for_django(structlog_processors=...)`
  can't be used in the usual "mutate-this-list" way; the Django app
  instead runs OTel setup in `AppConfig.ready()` and then reconfigures
  structlog explicitly to insert `otel_trace_context_processor` before its
  renderer. See `apps/django-app/demo/apps.py` for the pattern.
- **`DjangoInstrumentor` must not be listed manually in `MIDDLEWARE`.** It
  prepends its own server-span middleware automatically when
  `configure_otel_for_django` calls `.instrument()`.
- **`OpenTelemetry` global state is per-process.** Gunicorn sync/gthread
  workers re-run `AppConfig.ready()` per fork; the library's setup
  function is idempotent so this is safe.
