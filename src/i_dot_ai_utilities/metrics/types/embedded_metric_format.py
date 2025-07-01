from enum import Enum
from typing import TypedDict


class StorageResolution(Enum):
    """Sets the resolution at which metrics are stored in CloudWatch.

    :Standard: Graphing available at 60-second granularity (per-minute datapoints)
    :High: Stored at 1-second granularity (per-second datapoints)
    """

    STANDARD = 60
    HIGH = 1


class MetricDefinition(TypedDict):
    Name: str
    Unit: str
    StorageResolution: StorageResolution | int


class CloudWatchMetricBlock(TypedDict):
    Namespace: str
    Dimensions: list[list[str]]
    Metrics: list[MetricDefinition]


class AWSBlock(TypedDict):
    Timestamp: int
    CloudWatchMetrics: list[CloudWatchMetricBlock]


class EmbeddedMetricFormat(TypedDict):
    _aws: AWSBlock
