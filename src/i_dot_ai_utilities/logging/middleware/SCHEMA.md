# Django structured-logging middleware — event schema

**Version: `1.0`**

Bound as `logging_schema_version` on every event emitted by the middleware so
downstream consumers can detect breaking changes without inspecting source.
Version format: `MAJOR.MINOR`.

- **MAJOR** bump — a field is renamed, removed, or changes type/units.
- **MINOR** bump — a new field is added; no existing field is affected.

Consumers SHOULD pin against the major version in queries/dashboards that
rely on a specific shape. The library commits to backwards-compatible
MINOR bumps within a MAJOR line.

## Events

| Event | When | Level |
|---|---|---|
| `structured_logging_middleware_active` | Once per worker at middleware init | `info` |
| `structured_logging_middleware_forbidden_headers_rejected` | At init, if `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` names a header on the denylist | `warning` |
| `request_started` | Entering the view chain (non-excluded paths only) | `info` |
| `request_completed` | View returned normally, OR view raised `Http404` | status-driven: `info` (2xx/3xx), `warning` (4xx inc. Http404), `error` (5xx) |
| `request_failed` | View raised an unhandled non-`Http404` exception | `error` |

Exactly **one** `request_started` and **one** `request_completed`-or-`request_failed`
is emitted per non-excluded request — the middleware's `try/except/finally`
enforces this structurally (constitution Art. 47).

## Fields

Grouped by origin. Types are the logical types bound into `structlog.contextvars`;
downstream JSON encoding may coerce per the consumer's processor chain.

### Always present on `request_*` events

| Field | Type | Source | Description |
|---|---|---|---|
| `logging_schema_version` | `str` | middleware | This schema version (`"1.0"`) |
| `http.request.method` | `str` | enricher | Request method |
| `url.scheme` | `str` | enricher | `http` or `https` |
| `url.path` | `str` | enricher | Path component, capped at 2048 chars |
| `url.query` | `str` | enricher | Raw query string (may be empty), capped at 1024 chars |
| `server.address` | `str` | enricher | `Host` header / reverse-proxy `:authority` |
| `http.response.status_code` | `int` | middleware | Captured after view returns; synth `500` on exception, `404` on `Http404` |
| `duration_ms` | `int` | middleware | `time.monotonic()` delta, clamped to `>= 0` |
| `trace_id` | `str` | middleware | 32-hex or opaque id per precedence ladder below |
| `trace_id_source` | `str` | middleware | One of `traceparent` / `amzn` / `request_id` / `synthetic` |
| `request_id` | `str` | middleware | Freshly generated per-hop UUID4 hex, always distinct from `trace_id` |

### Conditionally present

| Field | Type | Emitted when | Description |
|---|---|---|---|
| `user_agent.original` | `str` | `User-Agent` header present | Capped at 512 chars |
| `client.address` | `str` | `REMOTE_ADDR` populated | Peer IP as Django sees it — NOT derived from `X-Forwarded-For` |
| `http.request.header.x_forwarded_for` | `str` | `X-Forwarded-For` header present | Verbatim raw value, capped at 512 chars |
| `http.route` | `str` | URL resolver matched a view | Dotted view name from `resolver_match.view_name` |
| `django.url_name` | `str` | URL resolver matched a view | `resolver_match.url_name` |
| `user.id` | `str` | Auth middleware has run AND `request.user.is_authenticated is True` | `str(request.user.pk)` (falls back to `id` only when `pk is None`) |
| `span_id` | `str` | `traceparent` parsed successfully | Lowercase 16-hex from the `parent-id` segment |
| `trace_flags` | `str` | `traceparent` parsed successfully | Masked 2-hex byte |
| `upstream_request_id` | `str` | `X-Request-ID` header present and structurally valid | Verbatim value, capped at 200 chars |
| `amzn_trace_root` | `str` | `X-Amzn-Trace-Id` with parseable `Root=` | Verbatim `Root=` value including the `1-` version prefix |
| `http.request.header.<name>` | `str` | Header in `I_DOT_AI_LOGGING_HEADER_ALLOWLIST` AND not in the denylist | Lowercased, hyphens to underscores, capped at 512 chars |
| `exception.type` | `str` | `request_failed` OR `Http404` completion | `type(exc).__name__` (simple class name) |
| `error.type` | `str` | Response status `>= 400`, OR exception path | Status code string (e.g. `"500"`) OR fully-qualified exception class name (e.g. `"myapp.errors.PaymentError"`) per OTel HTTP semconv |

### Startup-event-only fields

Bound on `structured_logging_middleware_active`:

| Field | Type | Description |
|---|---|---|
| `logger` | `str` | Class name of the resolved logger instance |
| `excluded_prefixes` | `list[str]` | Effective prefix-exclusion list |
| `excluded_regex_count` | `int` | Number of compiled regex exclusions |
| `header_allowlist_size` | `int` | Number of headers in the effective allowlist (post-denylist filter) |

Bound on `structured_logging_middleware_forbidden_headers_rejected`:

| Field | Type | Description |
|---|---|---|
| `rejected` | `list[str]` | Header names dropped because they appear in `FORBIDDEN_HEADER_NAMES` |

## Trace-id precedence

Resolved in this order; first match wins:

1. W3C `traceparent` (32-hex trace id, 16-hex span id, masked flags). Lowercase hex only, all-zero ids rejected, version `ff` rejected.
2. AWS `X-Amzn-Trace-Id` `Root=` segment (the `1-` version prefix is preserved; `Self=` is ignored).
3. `X-Request-ID` — only if the value is UUID-hex shaped. Opaque / attacker-chosen non-hex values are preserved as `upstream_request_id` but never promoted to `trace_id`.
4. Freshly generated UUID4 hex.

`request_id` is **always** a fresh UUID4 for this hop, distinct from `trace_id`.

## Security guarantees

- `Authorization`, `Cookie`, `Set-Cookie`, `Proxy-Authorization`, CSRF tokens, session identifiers, request bodies, and response bodies are NEVER logged, even if named in the allowlist. See `FORBIDDEN_HEADER_NAMES` in `_headers.py`.
- Header values capped at 512 chars, `url.path` at 2048, `url.query` at 1024.
- User identity is `user.id` only — never email, username, or other PII. `enduser.*` (used pre-1.0) has been removed to match OTel semconv v1.24.
- The middleware never calls `structlog.configure()` — directly or transitively.

## Migration from pre-1.0

- `enduser.id` → `user.id`. `enduser.authenticated` removed entirely (derive from presence of `user.id`).
- `error.type` is new. Consumers can rely on it for OTel-compatible HTTP error classification.
- `logging_schema_version` is new.
