from abc import ABC, abstractmethod
from typing import Any

class MetricsWriter(ABC):
    @abstractmethod
    def put_metric(self, name: str, value: float, dimensions: dict, unit: str = "Count") -> None:
        pass
