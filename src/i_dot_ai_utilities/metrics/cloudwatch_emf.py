import json
import sys
import time

from i_dot_ai_utilities.metrics.interfaces import MetricsWriter
from i_dot_ai_utilities.metrics.types.embedded_metric_format import (
    EmbeddedMetricFormat,
    StorageResolution,
)


class CloudwatchEmbeddedMetricsWriter(MetricsWriter):
    """Create a new CloudWatch Metrics Writer.

    Metrics are logged to stdout in the Embedded Metrics Format, which are automatically registered as time-series metrics by CloudWatch Logs.
    
    :param namespace: The namespace in CloudWatch in which to store all metrics. Usually the service/repo name, or some other app identifier.
    """  # noqa: E501

    def __init__(self, namespace: str):
        self.namespace = namespace

    def put_metric(
        self,
        metric_name: str,
        value: float,
        dimensions: dict | None = None,
        unit: str = "Count",
    ) -> None:
        """Put a time-series metric to CloudWatch.

        See the i.AI utils readme for full details on usage.

        :param metric_name: The name of the metric to log.
        :param value: The numerical metric value.
        :param dimensions: A k/v set of **low-cardinality** dimensions to add to the metric for graphing purposes.
        :param unit: The metric's unit, for example 'count', 'milliseconds', etc.
        """  # noqa: E501
        try:
            self._put_metric_internal(metric_name, value, dimensions, unit)
        except Exception as e:  # noqa: BLE001
            print(f"Failed to write metric: {e}")  # noqa: T201

    def _put_metric_internal(
        self,
        metric_name: str,
        value: float,
        dimensions: dict | None = None,
        unit: str = "Count",
    ) -> None:
        if not metric_name or not value:
            msg = "Missing required parameter"
            raise ValueError(msg)

        if (
            type(metric_name) is not str
            or type(value) not in [int, float]
            or type(unit) is not str
        ):
            msg = "Incorrect parameter type"
            raise ValueError(msg)

        dimensions = dimensions or {}
        dimension_names = list(dimensions.keys()) if dimensions else []

        emf: EmbeddedMetricFormat = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": self.namespace,
                        "Dimensions": [dimension_names] if dimension_names else [],
                        "Metrics": [
                            {
                                "Name": metric_name,
                                "Unit": unit,
                                "StorageResolution": StorageResolution.STANDARD.value,
                            }
                        ],
                    }
                ],
            },
            **dimensions,
        }

        metric_payload = {**emf, metric_name: value}

        print(json.dumps(metric_payload), file=sys.stdout)  # noqa: T201
