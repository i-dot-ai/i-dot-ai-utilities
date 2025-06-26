from typing import Any

from i_dot_ai_utilities.logging.types.fastapi_enrichment_schema import (
    ExtractedFastApiContext,
    RequestLike,
)


class FastApiEnricher:
    def extract_context(
        self, logger: Any, request: RequestLike
    ) -> ExtractedFastApiContext | None:
        try:
            self._validate_object_instance(request)

            return {
                "request_method": request.method,
                "request_base_url": str(request.base_url),
                "request_user_agent": request.headers.get("user-agent", "none"),
                "request_x_forwarded_for": request.headers.get(
                    "x-forwarded-for", "none"
                ),
                "request_path": request.url.path,
                "request_query": request.url.query,
            }
        except Exception:
            logger.exception("Exception(Logger): Failed to extract FastAPI fields")
            return None

    def _validate_object_instance(self, request: RequestLike) -> None:
        if not isinstance(request, RequestLike):
            msg = (
                "Exception(Logger): Request object doesn't conform to "
                "RequestLike. Context not set."
            )
            raise TypeError(msg)
