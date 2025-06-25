from typing import Any

from i_dot_ai_utilities.logging.enrichers.fargate_enricher import (
    FargateEnvironmentEnricher,
)
from i_dot_ai_utilities.logging.enrichers.fastapi_enricher import (
    FastApiEnricher,
    RequestLike,
)
from i_dot_ai_utilities.logging.types.enrichment_types import (
    ContextEnrichmentType,
    ExecutionEnvironmentType,
)
from i_dot_ai_utilities.logging.types.fargate_enrichment_schema import (
    ExtractedFargateContext,
)
from i_dot_ai_utilities.logging.types.fastapi_enrichment_schema import (
    ExtractedFastApiContext,
)


class EnrichmentProvider:
    _fast_api_enricher: FastApiEnricher
    _execution_environment_enricher: FargateEnvironmentEnricher | None
    _execution_environment_context_cache: ExtractedFargateContext | None = None
    _has_environment_context_extraction_ran = False

    def __init__(self, execution_environment: ExecutionEnvironmentType):
        self._fast_api_enricher = FastApiEnricher()

        match execution_environment:
            case ExecutionEnvironmentType.FARGATE:
                self._execution_environment_enricher = FargateEnvironmentEnricher()
            case _:
                self._execution_environment_enricher = None

    def extract_context_from_framework_enricher(
        self,
        self_logger: Any,
        enricher_type: ContextEnrichmentType,
        enricher_object: RequestLike,
    ) -> ExtractedFastApiContext | None:
        match enricher_type:
            case ContextEnrichmentType.FASTAPI:
                return self._fast_api_enricher.extract_context(
                    self_logger, enricher_object
                )
            case _:
                self_logger.exception(
                    (
                        "Exception(Logger): An enricher type of '{enricher_type}' was "
                        "not recognised, no context added."
                    ),
                    enricher_type=enricher_type,
                )
                return None

    def load_execution_environment_context(
        self, self_logger: Any
    ) -> ExtractedFargateContext | None:
        if self._execution_environment_enricher is None:
            return None

        if self._has_environment_context_extraction_ran:
            return self._execution_environment_context_cache

        self._execution_environment_context_cache = (
            self._execution_environment_enricher.extract_context(self_logger)
        )
        self._has_environment_context_extraction_ran = True
        return self._execution_environment_context_cache
