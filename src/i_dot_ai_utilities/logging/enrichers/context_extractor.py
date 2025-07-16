from abc import ABC, abstractmethod
from typing import Any

from i_dot_ai_utilities.logging.types.fargate_enrichment_schema import (
    ExtractedFargateContext,
)
from i_dot_ai_utilities.logging.types.lambda_enrichment_schema import (
    ExtractedLambdaEnvironmentMetadata,
)

ExtractedContextResult = (
    ExtractedFargateContext | ExtractedLambdaEnvironmentMetadata | None
)


class BaseEnvironmentEnricher(ABC):
    @abstractmethod
    def extract_context(self, self_logger: Any) -> ExtractedContextResult:
        pass
