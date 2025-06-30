import json
import sys
import time
from typing import Dict
from i_dot_ai_utilities.metrics.interfaces import MetricsWriter
from i_dot_ai_utilities.metrics.types.embedded_metric_format import EmbeddedMetricFormat, StorageResolution


class CloudwatchEmbeddedMetricsWriter(MetricsWriter):
    def __init__(self, namespace: str):
        self.namespace = namespace

    def put_metric(
            self,
            metric_name: str,
            value: int | float,
            dimensions: Dict | None = None,
            unit: str = "Count",
        ) -> None:
        try:
            self._put_metric_internal(metric_name, value, dimensions, unit)
        except Exception as e:
            print(f"Failed to write metric: {e}")

    def _put_metric_internal(self,
            metric_name: str,
            value: int | float,
            dimensions: Dict | None = None,
            unit: str = "Count",
        ) -> None:
            if not metric_name or not value:
                raise ValueError('Missing required parameter')
            
            if (type(metric_name) is not str 
                    or type(value) not in [int, float]
                    or type(unit) is not str
                    ):
                raise ValueError('Incorrect parameter type')

            dimensions = dimensions or {}
            dimension_names = list(dimensions.keys()) if dimensions else []

            emf: EmbeddedMetricFormat = {
                "_aws": {
                    "Timestamp": int(time.time() * 1000),
                    "CloudWatchMetrics": [
                        {
                            "Namespace": self.namespace,
                            "Dimensions": [dimension_names] if dimension_names else [],
                            "Metrics": [{
                                "Name": metric_name,
                                "Unit": unit,
                                "StorageResolution": StorageResolution.STANDARD.value,
                            }]
                        }
                    ]
                },
                **dimensions,
            }

            metric_payload = {
                **emf,
                metric_name: value
            }

            print(json.dumps(metric_payload), file=sys.stdout)
