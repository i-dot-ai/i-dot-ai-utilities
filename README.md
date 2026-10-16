# i.AI Utility Code

`i-dot-ai-utilities` is a python package used and developed by the i.AI team within DSIT.
It provides common features used in many of our applications.

## Installation

When installing the package, the base package comes with only the `logger` module, to install more use `extras`. The following extras are available:

- auth
- file_store
- litellm
- metrics
- otel _(pre-release)_
- otel_django _(pre-release)_
- otel_fastapi _(pre-release)_
- otel_native _(pre-release)_
- otel_lambda _(pre-release)_
- django _(pre-release)_
- all

To install the package, use your package manager of choice:

```bash
pip install "i-dot-ai-utilities[all]"

poetry add "i-dot-ai-utilities[all]"

uv pip install "i-dot-ai-utilities[all]"
```

Replace `[all]` with any extras from the list above, comma separated, or remove entirely to install just the base package.

> ⚠️ **The `otel*` and `django` extras are pre-release; pass `--pre` (or pin the exact pre-release version) to install them.** `[otel_native]` and `[otel_lambda]` target the `otel-gateway` logs pipeline via `i_dot_ai_utilities.otel.configure_otel`. The legacy `[otel]`, `[otel_django]`, `[otel_fastapi]`, and `[django]` extras remain on the existing structlog/PoC bridge until the container follow-ups land. `[all]` pulls in the pre-release extras too, so it also requires `--pre` during the pre-release window.

## Features

### Current features:

#### Structured Logging

The structured logging library is used to generate logs in a known format so they can be further processed into logging systems downstream. It also provides the ability to easily enrich log messages with useful data, and in some cases does this automatically.

You can find information on usage of the logging library in the [logging library readme](./src/i_dot_ai_utilities/logging/README.md).

#### Metrics Collection

The metrics collection library provides the ability to write time-series metrics out to useful destinations. In the case of i.AI, this is CloudWatch Metrics.

There's also a handy interface provided which can be used in your code to allow for modularity if the swapping out of implementations is desired.

You can find information on usage of the metrics collection library in the [metrics library readme](./src/i_dot_ai_utilities/metrics/README.md).

#### File store

The file store library currently only supports aws s3 as this is the main/only file store that we use in anger.

It can be used to upload and download files, and generate file download links for end-users to use.

The aim is to be able to plug more file storage destinations into this module so it can be swapped out easily.

You can find out information on usage of the file store library in the [file store library readme](./src/i_dot_ai_utilities/file_store/README.md).

#### LiteLLM

This library currently supports LLM proxy through LiteLLM, for chat and embedding functions.

The hope for this library is to easily swap between proxies for whichever is best-in-market at the time.

As the end-user, you'll have to make sure that the API key issued to you by LiteLLM will support the models you're trying to use.

More information on usage and setup can be found in the [litellm library readme](./src/i_dot_ai_utilities/litellm/README.md).

#### OpenTelemetry & Django middleware

For Python AWS Lambdas sending structured logs to the internal `otel-gateway`, install `[otel_lambda]` and call `i_dot_ai_utilities.otel.configure_otel` after defining your handler. That native path configures the standard OpenTelemetry SDK, Python `logging` handler, AWS Lambda and botocore auto-instrumentation, synchronous per-invocation log flushing, and an outage circuit breaker without routing through `StructuredLogger`. `[otel_native]` provides the common SDK and OTLP/HTTP dependencies without the Lambda instrumentation packages.

The legacy `otel` extra (`i_dot_ai_utilities.logging.otel`) remains unchanged for the existing framework-bridge track (`configure_otel_for_django` / `configure_otel_for_fastapi` / `configure_otel_for_lambda`) that wires traces, metrics, and the structlog log bridge. Add `otel_django` or `otel_fastapi` for the matching auto-instrumentation.

The `django` extra adds request-lifecycle middleware: `StructuredLoggingMiddlewareOTel` for per-request structured logging and `DjangoUserIdMiddleware` for authenticated-user attribution. For a Django app on the legacy bridge track, install `[django,otel_django]`.

> ⚠️ **Pre-release** (see the [Installation](#installation) note): `[otel_native]` and `[otel_lambda]` target the `otel-gateway` logs pipeline; the existing `[otel]`, `[otel_django]`, `[otel_fastapi]`, and `[django]` bridge extras stay on their PoC behaviour until the container follow-ups.

You can find usage details in the [logging library readme](./src/i_dot_ai_utilities/logging/README.md).

### Future features:

- authorisation
- vector stores

## How to use

### Unit Testing

All modules contained within this repo include robust test suites. You can run tests for all modules in this package using `make test`.

Tests and linting runs on every push and merge to main.

When making changes or adding tests, please ensure tests run in isolation, as failures of external dependencies will impact the CI tests for all packages. Please also make sure that tests pass before merging, as failing tests will impact every package in the application.

### CI/CD & Releases

Package versions are published to PyPI by the `Publish python package` workflow. Version names must adhere to semantic versioning and must *not* be prefixed with a `v`. Merging to `main` does **not** publish anything; publishing is always a deliberate action.

There are three publish channels:

| Channel | How to trigger | Version stamped | Published to |
|---|---|---|---|
| **Stable release** | Create a GitHub Release, **not** marked as a pre-release | bare tag (e.g. `0.6.0`) | production PyPI |
| **Pre-release → production** | Run the workflow manually (`Actions → Publish python package → Run workflow`) with `pypi=production` and an existing pre-release tag | tag verbatim (e.g. `0.6.0rc1`) | production PyPI |
| **Pre-release → test** | Create a GitHub Release, marked as a pre-release | `<tag>.<timestamp>` | Test PyPI |

Notes:

- **Manual dispatch is pre-release only.** The workflow rejects a manual `production` publish whose version is not a PEP 440 pre-release (an `rc` / `a` / `b` / `.dev` suffix). Cut stable releases through the GitHub Release flow.
- **The tag must already exist before a manual dispatch.** The workflow checks out the tag you pass; create and push it first.

#### Installing a pre-release

**From production PyPI** (pre-release → production channel): pip will not resolve a PEP 440 pre-release unless you opt in with `--pre` or pin the exact version, so ordinary consumers stay on the latest stable automatically.

```bash
# opt in to pre-releases (resolves the newest rc)
pip install --pre "i-dot-ai-utilities[django,otel_django]"

# or pin the exact pre-release version
pip install "i-dot-ai-utilities[django,otel_django]==0.6.0rc1"
```

**From Test PyPI** (pre-release → test channel): point your installer at Test PyPI, provide production PyPI as a fallback index so runtime dependencies resolve, and pin the exact **timestamped** version the workflow generated (replace the version as required):

```bash
uv pip install --index https://test.pypi.org/simple/ --index-strategy unsafe-best-match "i-dot-ai-utilities[django,otel_django]==0.1.1rc202506301522"
```

Or with pip:

```bash
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ "i-dot-ai-utilities[django,otel_django]==0.1.1rc202506301522"
```

`--pre` alone is not enough for the Test PyPI channel: the package only exists on Test PyPI, so you must also point the installer at that index (and keep production PyPI as a fallback for dependencies).

## Licence

MIT
