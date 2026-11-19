from abc import ABC, abstractmethod

from i_dot_ai_utilities.metrics.unit import Unit


class MetricsWriter(ABC):
    @abstractmethod
    def put_metric(
        self,
        metric_name: str,
        value: float,
        dimensions: dict | None = None,
        unit: str = Unit.Count,
    ) -> None:
        pass
