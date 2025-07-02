# Metrics

A library for writing time-series metrics to CloudWatch (which subsequently makes them available for graphing in Grafana). Emit your business and performance metrics with a few lines of code.

The writer uses CloudWatch Embedded Metric Format, which is a specification developed by AWS to make the writing of time-series metrics trivial (as long as the code is running on AWS!). The formatted message is written to stdout and picked up by CloudWatch Logs for processing.

## Usage

### Create a Metrics Writer

You can create the most basic version of the metrics writer by simply instantiating one with a few parameters:
```python
from i_dot_ai_utilities.metrics.cloudwatch import CloudwatchEmbeddedMetricsWriter

metrics = CloudwatchEmbeddedMetricsWriter(
    namespace=os.environ['REPO'],
    environment=os.environ['ENVIRONMENT'],
)

...

metric.put_metric(...) # See examples below
```

The namespace is the storage location for the metrics, and should be the name of the app. This is combined with the environment name to create a metrics store for a given app and execution environment.

These two methods are enough to load time-series metrics into CloudWatch, for further graphing elsewhere in other tools (such as Grafana).

<br>

***

<br>

You may also want to wrap instantiation inside an interface to make your code more modular in case other users want to swap out the underlying implementation in future. The library provides an interface for this for ease of use purposes (or you could create your own):
```python
from i_dot_ai_utilities.metrics.cloudwatch import CloudwatchEmbeddedMetricsWriter
from i_dot_ai_utilities.metrics.interfaces import MetricsWriter

def get_metric_writer() -> MetricsWriter:
    # This could also be a switch-case or factory class
    return CloudwatchEmbeddedMetricsWriter(
        namespace=os.environ['REPO'], 
        environment=os.environ['ENVIRONMENT'],
    )

metrics = get_metric_writer()
```

<br>

***

<br>

### Creating Metrics

Most simple metrics are made up of two elements - the name of the metric (which becomes its identifier in CloudWatch), and the metric value. The value is always numerical - either an integer or float.

This example creates metric based on login success, incrementing the value by one each time. This counter can then be graphed with the 'Sum' statistic to get the number of logins for a given time period.
```python
def login():
    try:
        # login happens
        metrics.put_metric(
            name="successful_logins"
            value=1
        )
    except NotAuthorisedError: 
        # login unauthorised
        metrics.put_metric(
            name="failed_logins"
            value=1
        )        
    except Exception:
        # an unhandled exception occurred
        metrics.put_metric(
            name="unhandled_login_errors"
            value=1
        )
```

<br>

***

<br>

Time-based metrics can also be emitted to get the performance of business-level operations within the app. These floats can then be graphed with stats such as 'Average' or 'p95' to visualise and alert on how the service is performing in the context of a given operation or code path.
```python
def do_expensive_operation():
    timer_start = datetime.datetime.now()
    # Compute lots of things
    timer_end = datetime.datetime.now()

    timer_result_ms = (timer_end - timer_start).total_seconds() * 1000

    metrics.put_metric(
        metric_name="expensive_operation_duration_ms",
        value=timer_result_ms,
    )
```

<br>

***

<br>

### Adding Dimensions (Advanced Usage)

For almost all use cases, the ability to write simple metrics will be enough for us to graph them and derive the information we need from them. However, it's also possible to add dimensions to a metric.

**This should be used carefully**, as the use of high-cardinality values here (such as requestId, guid etc) will cause a new metric to be created within CloudWatch for each new dimension value (versus adding time-series data to an existing metric), which could cause a huge amount of unique metrics to be stored by Cloudwatch. This has very large cost implications. Speak to the Platform team if you want a hand with this feature.

This example shows how a dimension might be added to allow for the breakdown of a single metric:
```python
def process_serviceX_and_respond_to_user(request):
    try:
        response = do_something(request)
        metrics.put_metric(
            metric_name="serviceX_processor_result",
            value=1,
            dimensions={
                "Result": "Healthy"
            }
        )
        return response
    except ClientError:
        metrics.put_metric(
            metric_name="serviceX_processor_result",
            value=1,
            dimensions={
                "Result": "ClientError"
            }
        )
        return client_exception(code=400)
    except Exception:
        metrics.put_metric(
            metric_name="serviceX_processor_result",
            value=1,
            dimensions={
                "Result": "ServerError"
            }
        )
        return server_exception(code=500)
```

As a rule-of-thumb, metrics collection in CloudWatch should have 'holistic' granularity, and the use of excessive dimensions should be avoided. If the analysis of high-cardinality metrics is required, it's better to use the structured logging library contained in this package to emit granular logs. These can then be analysed in OpenSearch, which is much more suited for fine-grained analysis.

<br>

***

<br>

### Variable interpolation in metric names (Advanced Usage)

It's also (naturally) possible to interpolate variables into the metric name, and you may want to do this in some cases (e.g. for DRYness).

```python
metrics.put_metric(
    metric_name=f"{service_name}_success",
    value=1,
)
```

In this case, the same warning for when using dimensions applies here - care should be taken to ensure the `service_name` variable (e.g.) can't spawn too many permutations, or it could cause an explosion in the amount of different metrics stored by CloudWatch. 