from typing import Any, cast

from i_dot_ai_utilities.logging.enrichers.context_extractor import (
    ExtractedContextResult,
)
from i_dot_ai_utilities.logging.enrichers.fargate_enricher import (
    FargateEnvironmentEnricher,
)
from i_dot_ai_utilities.logging.enrichers.fastapi_enricher import (
    FastApiEnricher,
    RequestLike,
)
from i_dot_ai_utilities.logging.enrichers.lambda_context_enricher import (
    LambdaContextEnricher,
)
from i_dot_ai_utilities.logging.enrichers.lambda_environment_enricher import (
    LambdaEnvironmentEnricher,
)
from i_dot_ai_utilities.logging.types.enrichment_types import (
    ContextEnrichmentType,
    ExecutionEnvironmentType,
)
from i_dot_ai_utilities.logging.types.fastapi_enrichment_schema import (
    ExtractedFastApiContext,
)
from i_dot_ai_utilities.logging.types.lambda_enrichment_schema import (
    ExtractedLambdaContext,
    LambdaContextLike,
)


class EnrichmentProvider:
    _fast_api_enricher: FastApiEnricher
    _lambda_enricher: LambdaContextEnricher
    _execution_environment_enricher: (
        FargateEnvironmentEnricher | LambdaEnvironmentEnricher | None
    )
    _execution_environment_context_cache: ExtractedContextResult = None
    _has_environment_context_extraction_ran = False

    def __init__(self, execution_environment: ExecutionEnvironmentType):
        self._fast_api_enricher = FastApiEnricher()
        self._lambda_enricher = LambdaContextEnricher()

        match execution_environment:
            case ExecutionEnvironmentType.FARGATE:
                self._execution_environment_enricher = FargateEnvironmentEnricher()
            case ExecutionEnvironmentType.LAMBDA:
                self._execution_environment_enricher = LambdaEnvironmentEnricher()
            case _:
                self._execution_environment_enricher = None

    def extract_context_from_framework_enricher(
        self,
        self_logger: Any,
        enricher_type: ContextEnrichmentType,
        enricher_object: RequestLike | LambdaContextLike,
    ) -> ExtractedFastApiContext | ExtractedLambdaContext | None:
        match enricher_type:
            case ContextEnrichmentType.FASTAPI:
                return self._fast_api_enricher.extract_context(
                    self_logger, cast("RequestLike", enricher_object)
                )
            case ContextEnrichmentType.LAMBDA:
                return self._lambda_enricher.extract_context(
                    self_logger, cast("LambdaContextLike", enricher_object)
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
    ) -> ExtractedContextResult:
        if self._execution_environment_enricher is None:
            return None

        if self._has_environment_context_extraction_ran:
            return self._execution_environment_context_cache

        self._execution_environment_context_cache = (
            self._execution_environment_enricher.extract_context(self_logger)
        )
        self._has_environment_context_extraction_ran = True
        return self._execution_environment_context_cache
