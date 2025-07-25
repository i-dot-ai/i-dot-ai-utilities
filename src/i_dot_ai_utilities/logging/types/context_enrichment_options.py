from typing import TypedDict

from i_dot_ai_utilities.logging.enrichers.enrichment_provider import (
    ContextEnrichmentType,
)
from i_dot_ai_utilities.logging.enrichers.fastapi_enricher import RequestLike
from i_dot_ai_utilities.logging.types.lambda_enrichment_schema import LambdaContextLike


class ContextEnrichmentOptions(TypedDict):
    type: ContextEnrichmentType
    object: RequestLike | LambdaContextLike
