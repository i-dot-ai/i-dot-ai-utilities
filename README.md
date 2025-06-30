# i.AI Utility Code

`i-dot-ai-utilities` is a python package used and developed by the i.AI team within DSIT.
It provides common features used in many of our applications.

## Features

### Current features:

#### Structured Logging

The structured logging library is used to generate logs in a known format so they can be further processed into logging systems downstream. It also provides the ability to easily enrich log messages with useful data, and in some cases does this automatically. 

You can find information on usage of the logging library in the [logging library readme](./src/i_dot_ai_utilities/logging/README.md).

#### Metrics Collection

The metrics collection library provides the ability to write time-series metrics out to useful destinations. In the case of i.AI, this is CloudWatch Metrics.

There's also a handy interface provided which can be used in your code to allow for modularity if the swapping out of implementations is desired.

You can find information on usage of the metrics collection library in the [metrics library readme](./src/i_dot_ai_utilities/metrics/README.md).

### Future features:

- keycloak authentication
- s3
- langfuse and litellm
- logging/observability
- opensearch

## Settings

This is where some of the above can be found:


## How to use

## Licence

MIT
