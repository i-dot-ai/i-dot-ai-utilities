# Observability ecosystem

`src/i_dot_ai_utilities/otel` is the Python producer interface for the i.AI OpenTelemetry stack. The hub is [core-logging](https://github.com/i-dot-ai/core-logging), and its [AGENTS.md](https://github.com/i-dot-ai/core-logging/blob/main/AGENTS.md) maps every repository in the stack.

When you change the `OTEL_*` environment variables this package reads, its OTLP protocol or defaults, or its attribute conventions, check:

- [i-dot-ai-core-terraform-modules](https://github.com/i-dot-ai/i-dot-ai-core-terraform-modules), which injects those variables
- [core-logging](https://github.com/i-dot-ai/core-logging), for collector config and redaction
- [i-dot-ai-utilities-npm](https://github.com/i-dot-ai/i-dot-ai-utilities-npm) (`packages/observability`), for Node.js parity
