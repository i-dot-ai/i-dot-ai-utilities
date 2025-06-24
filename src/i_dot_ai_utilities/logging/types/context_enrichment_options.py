from typing import TypedDict

from i_dot_ai_utilities.logging.enrichers.enrichment_provider import (
    ContextEnrichmentType,
)
from i_dot_ai_utilities.logging.enrichers.fastapi_enricher import RequestLike


class ContextEnrichmentOptions(TypedDict):
    type: ContextEnrichmentType
    object: RequestLike
