# i.AI utility code

`i-dot-ai-utilities` is a Python package developed and maintained by the i.AI team. It provides shared modules used across i.AI applications.

## Install the package

The base package includes only the `logging` module. Install optional extras to include additional modules:

- `auth`
- `file_store`
- `litellm`
- `metrics`
- `otel_native` _(pre-release)_
- `otel_native_lambda` _(pre-release)_
- `otel_native_botocore` _(pre-release)_
- `otel_native_fastapi` _(pre-release)_
- `all` _(includes pre-release extras)_

To install the base package or stable extras (`auth`, `file_store`, `litellm`, and `metrics`), run one of these commands:

```bash
pip install "i-dot-ai-utilities[auth]"

poetry add "i-dot-ai-utilities[auth]"

uv pip install "i-dot-ai-utilities[auth]"
```

Replace `[auth]` with a comma-separated list of extras, or omit the brackets to install only the base `logging` module.

> **Pre-release extras:** The `otel*`, `django`, and `all` extras require pre-release installation (pass `--pre` with `pip`, `--allow-prereleases` with `poetry`, `--prerelease=allow` with `uv`, or pin the exact pre-release version). Use the `otel_native*` extras (`i_dot_ai_utilities.otel`) for new OpenTelemetry work. The legacy `otel`, `otel_django`, `otel_fastapi`, and `django` extras belong to the older structlog bridge and will be removed.

To install a pre-release extra such as `otel_native_fastapi` or `all`:

```bash
pip install --pre "i-dot-ai-utilities[otel_native_fastapi]"

poetry add --allow-prereleases "i-dot-ai-utilities[otel_native_fastapi]"

uv pip install --prerelease=allow "i-dot-ai-utilities[otel_native_fastapi]"
```

## Features

### Current features

#### Structured logging (deprecated)

The `logging` module formats logs for downstream log pipelines and enriches log messages with runtime and request metadata.

Read the [logging module README](./src/i_dot_ai_utilities/logging/README.md) for usage details.

#### Metrics collection

The `metrics` module writes time-series metrics to CloudWatch Metrics using Embedded Metric Format (EMF). It also defines a `MetricsWriter` interface so you can swap implementations without changing caller code.

Read the [metrics module README](./src/i_dot_ai_utilities/metrics/README.md) for usage details.

#### File store

The `file_store` module supports uploading and downloading files and generating download links. It currently targets AWS S3, with an interface designed for additional storage backends.

Read the [file store module README](./src/i_dot_ai_utilities/file_store/README.md) for usage details.

#### LiteLLM

The `litellm` module wraps the LiteLLM proxy for chat and embedding calls so applications can switch proxy configurations without changing call sites. Ensure that your LiteLLM API key grants access to the models you request.

Read the [LiteLLM module README](./src/i_dot_ai_utilities/litellm/README.md) for setup and usage details.

#### OpenTelemetry

Use `i_dot_ai_utilities.otel` for new work. Install `[otel_native]` for plain containers or scripts, or install a platform extra (`[otel_native_lambda]`, `[otel_native_botocore]`, or `[otel_native_fastapi]`), which already includes `otel_native`. Call `configure_otel()` at startup. It detects AWS Lambda, ECS (Fargate or EC2), AWS Batch, or local runs, sends Python `logging` output and traces to `otel-gateway` over OTLP, enables installed instrumentation libraries, and enforces the `i_ai` namespace for custom attributes.

Read the [OpenTelemetry module README](./src/i_dot_ai_utilities/otel/README.md) for usage details.

#### Legacy OpenTelemetry bridge and Django middleware (deprecated)

The `otel` extra (`i_dot_ai_utilities.logging.otel`) and the `django` middleware belong to the earlier structlog-based bridge. They will be removed. Do not use them for new services. See the [logging module README](./src/i_dot_ai_utilities/logging/README.md) for existing consumers.

### Future features

- Vector stores

## Develop and test

### Run unit tests

Every module in this repository includes a unit test suite. Run all module test suites with `make test`.

Tests and linting run on every push and merge to `main`.

Keep tests isolated so external dependency failures do not affect other modules in CI. Verify that all tests pass before merging.

### CI and releases

The `Publish python package` workflow publishes package versions to PyPI. Version tags must follow semantic versioning without a leading `v`. Merging to `main` does not publish a release. Publishing is always a manual action.

| Channel | How to trigger | Version stamped | Published to |
|---|---|---|---|
| **Stable release** | Create a GitHub Release that is not marked as a pre-release | Bare tag (for example `0.6.0`) | Production PyPI |
| **Pre-release to production** | Run the workflow manually (**Actions**, **Publish python package**, **Run workflow**) with `pypi=production` and an existing pre-release tag | Tag verbatim (for example `0.6.0rc1`) | Production PyPI |
| **Pre-release to test** | Create a GitHub Release marked as a pre-release | `<tag>.<timestamp>` | Test PyPI |

Read [`RELEASING.md`](./RELEASING.md) for the full pre-release, Test PyPI, and stable release runbook, including tag rules, local verification, and consumer install commands.

## Licence

MIT
