from enum import Enum
from typing import TypedDict, List, Optional


class StorageResolution(Enum):
    STANDARD = 60
    HIGH = 1


class MetricDefinition(TypedDict):
    Name: str
    Unit: str
    StorageResolution: Optional[StorageResolution]


class CloudWatchMetricBlock(TypedDict):
    Namespace: str
    Dimensions: List[List[str]]
    Metrics: List[MetricDefinition]


class AWSBlock(TypedDict):
    Timestamp: int
    CloudWatchMetrics: List[CloudWatchMetricBlock]


class EmbeddedMetricFormat(TypedDict):
    _aws: AWSBlock