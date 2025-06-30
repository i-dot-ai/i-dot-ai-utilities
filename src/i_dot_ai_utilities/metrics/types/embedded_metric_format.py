from enum import Enum
from typing import TypedDict


class StorageResolution(Enum):
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
