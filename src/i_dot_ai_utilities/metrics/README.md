# Metrics

`i_dot_ai_utilities.metrics` writes time-series metrics to CloudWatch so you can graph them in Grafana.

`CloudwatchEmbeddedMetricsWriter` uses the AWS CloudWatch Embedded Metric Format (EMF). When your code runs on AWS, the writer prints each formatted metric payload to stdout, and CloudWatch Logs extracts the time-series metric automatically.

## Usage

### Create a metrics writer

Instantiate `CloudwatchEmbeddedMetricsWriter` with a namespace, an environment name, and a `StructuredLogger` instance (used to log write failures):

```python
import os

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.metrics.cloudwatch import CloudwatchEmbeddedMetricsWriter

logger = StructuredLogger()
metrics = CloudwatchEmbeddedMetricsWriter(
    namespace=os.environ["REPO"],
    environment=os.environ["ENVIRONMENT"],
    logger=logger,
)

metrics.put_metric(metric_name="successful_logins", value=1)
```

Use your application name as `namespace`. The writer combines `namespace` and `environment` into `<namespace>/<environment>` to separate metrics by application and deployment environment.

Instantiating the writer and calling `put_metric()` is enough to publish time-series metrics to CloudWatch for graphing in Grafana.

To keep your code decoupled from the CloudWatch implementation, return the `MetricsWriter` interface from a factory function:

```python
import os

from i_dot_ai_utilities.logging.structured_logger import StructuredLogger
from i_dot_ai_utilities.metrics.cloudwatch import CloudwatchEmbeddedMetricsWriter
from i_dot_ai_utilities.metrics.interfaces import MetricsWriter


def get_metric_writer() -> MetricsWriter:
    logger = StructuredLogger()
    return CloudwatchEmbeddedMetricsWriter(
        namespace=os.environ["REPO"],
        environment=os.environ["ENVIRONMENT"],
        logger=logger,
    )


metrics = get_metric_writer()
```

### Create metrics

A metric requires two arguments: `metric_name` (the identifier in CloudWatch) and `value` (an integer or float).

This example emits login counters. You can graph each counter with the `Sum` statistic to count events over a time period:

```python
def login() -> None:
    try:
        # Run login logic.
        metrics.put_metric(
            metric_name="successful_logins",
            value=1,
        )
    except NotAuthorisedError:
        metrics.put_metric(
            metric_name="failed_logins",
            value=1,
        )
    except Exception:
        metrics.put_metric(
            metric_name="unhandled_login_errors",
            value=1,
        )
```

You can also emit duration metrics to measure operation performance. Graph these floats with statistics such as `Average` or `p95` to track and alert on latency:

```python
import time

from i_dot_ai_utilities.metrics.unit import Unit


def do_expensive_operation() -> None:
    timer_start = time.monotonic()
    # Run the operation.
    timer_result_ms = (time.monotonic() - timer_start) * 1000

    metrics.put_metric(
        metric_name="expensive_operation_duration_ms",
        value=timer_result_ms,
        unit=Unit.Milliseconds,
    )
```

### Add units

Pass the `unit` argument to label a measurement. `unit` defaults to `Unit.Count` (`"Count"`). Units do not convert values: `unit=Unit.Seconds` expects a value already measured in seconds.

Import `Unit` from `i_dot_ai_utilities.metrics.unit` ([unit.py](./unit.py)), which defines members such as `Unit.Seconds`, `Unit.Milliseconds`, `Unit.Percent`, and `Unit.CountsPerSecond`. For ratios or measurements without a supported unit, use `Unit.None_` (`"None"`). `CloudwatchEmbeddedMetricsWriter` also accepts strings matching [CloudWatch unit names](https://docs.aws.amazon.com/AmazonCloudWatch/latest/APIReference/API_MetricDatum.html), case-insensitively. If you pass an invalid unit, the writer logs a write failure and emits no metric.

```python
from i_dot_ai_utilities.metrics.unit import Unit

metrics.put_metric(metric_name="failure_rate", value=0.0, unit=Unit.None_)
metrics.put_metric(metric_name="processing_time_seconds", value=1.5, unit=Unit.Seconds)
```

### Add dimensions

Most use cases need only metric names and values. When you need to break down a metric across a small, fixed set of categories, pass a `dimensions` dictionary.

**Use dimensions carefully.** CloudWatch creates a separate billable metric for every unique dimension value combination instead of appending to an existing series. High-cardinality values such as request IDs or GUIDs can create thousands of metrics and cause large CloudWatch bills. Contact the Platform team if you need advice before adding dimensions.

This example uses a low-cardinality `Result` dimension to split outcomes of a single metric:

```python
def process_service_x_and_respond(request):
    try:
        response = do_something(request)
        metrics.put_metric(
            metric_name="service_x_processor_result",
            value=1,
            dimensions={"Result": "Healthy"},
        )
        return response
    except ClientError:
        metrics.put_metric(
            metric_name="service_x_processor_result",
            value=1,
            dimensions={"Result": "ClientError"},
        )
        return client_exception(code=400)
    except Exception:
        metrics.put_metric(
            metric_name="service_x_processor_result",
            value=1,
            dimensions={"Result": "ServerError"},
        )
        return server_exception(code=500)
```

Keep CloudWatch metrics coarse-grained and limit the number of dimensions. When you need fine-grained, high-cardinality analysis, emit structured application logs through `i_dot_ai_utilities.otel` (see the [OpenTelemetry guide](../otel/README.md)) and query them in Grafana and Loki.

### Interpolate variables in metric names

You can interpolate a variable into `metric_name` to share metric code across services:

```python
metrics.put_metric(
    metric_name=f"{service_name}_success",
    value=1,
)
```

The same cardinality warning applies to dynamic metric names. Ensure that `service_name` comes from a small, bounded set of values so it cannot create an unbounded number of CloudWatch metrics.
