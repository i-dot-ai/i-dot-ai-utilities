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
            name: str,
            value: int | float,
            dimensions: Dict | None = None,
            unit: str = "Count",
        ) -> None:
            
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
                                "Name": name,
                                "Unit": unit,
                                "StorageResolution": StorageResolution.STANDARD,
                            }]
                        }
                    ]
                },
                **dimensions,
            }

            metric_payload = {
                **emf,
                name: value
            }

            print(json.dumps(metric_payload), file=sys.stdout)
