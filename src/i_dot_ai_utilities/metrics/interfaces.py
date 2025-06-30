from abc import ABC, abstractmethod


class MetricsWriter(ABC):
    @abstractmethod
    def put_metric(
        self,
        metric_name: str,
        value: float,
        dimensions: dict | None,
        unit: str = "Count",
    ) -> None:
        pass
