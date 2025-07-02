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

### Unit Testing

It's important that packages include robust test suites. As well as the usual benefit of providing the confidence and ability to make rapid change without causing a regression, it's especially important here as the code in this repository will be used ubiquitously across our many applications. 

Tests and linting runs on every push and merge to main. These must pass before merging as failing tests will impact every package in the application.

Tests must run in isolation for the same reason, as failures of external dependencies will impact the CI tests for all packages.

### CI/CD & Releases

Releases must be manually created, after which the specified package version will be released to PyPI. As such, release names must adhere to semantic versioning. They must *not* be prefixed with a `v`.

You may release a pre-release tag to the test version of PyPI by specifying the release as a pre-release on creation. This allows for the testing of a tag in a safe environment before merging to main.

To test a pre-release tag, you can follow these steps in a repo of your choice:
1. Update pyproject.toml:
```
[[tool.poetry.source]]
name = "test-pypi"
url = "https://test.pypi.org/simple/"
priority = "supplemental"
```
2. Load the specific version into the environment (replacing version number as required)
```
poetry add --source test-pypi i-dot-ai-utilities==0.1.1rc202506301522
```



## Licence

MIT
