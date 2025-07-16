from typing import Any

from i_dot_ai_utilities.logging.types.lambda_enrichment_schema import (
    ExtractedLambdaContext,
    LambdaContextLike,
)


class LambdaContextEnricher:
    def extract_context(
        self, logger: Any, request: LambdaContextLike
    ) -> ExtractedLambdaContext | None:
        response: ExtractedLambdaContext | None = None
        try:
            self._validate_object_instance(request)

            response = {
                "lambda_context": {
                    "request_id": request.aws_request_id,
                    "function_arn": request.invoked_function_arn,
                }
            }
        except Exception:
            logger.exception(
                "Exception(Logger): Failed to extract Lambda Context fields"
            )
            return None
        else:
            return response

    def _validate_object_instance(self, request: LambdaContextLike) -> None:
        if not isinstance(request, LambdaContextLike):
            msg = (
                "Exception(Logger): Object doesn't conform to "
                "LambdaContextLike. Context not set."
            )
            raise TypeError(msg)
