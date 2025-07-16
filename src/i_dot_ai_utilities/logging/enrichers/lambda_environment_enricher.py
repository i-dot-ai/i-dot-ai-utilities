from functools import lru_cache
from typing import Any

from pydantic import Field
from pydantic_settings import BaseSettings

from i_dot_ai_utilities.logging.enrichers.context_extractor import (
    BaseEnvironmentEnricher,
)
from i_dot_ai_utilities.logging.types.lambda_enrichment_schema import (
    ExtractedLambdaEnvironmentMetadata,
)


class LambdaEnvironmentSettings(BaseSettings):
    aws_default_region: str = Field()
    aws_lambda_function_name: str = Field()


@lru_cache
def load_lambda_environment_variables() -> LambdaEnvironmentSettings:
    return LambdaEnvironmentSettings()  # type: ignore[call-arg]


class LambdaEnvironmentEnricher(BaseEnvironmentEnricher):
    def extract_context(
        self, self_logger: Any
    ) -> ExtractedLambdaEnvironmentMetadata | None:
        response: ExtractedLambdaEnvironmentMetadata | None = None
        try:
            loaded_metadata = load_lambda_environment_variables()

            response = {
                "lambda_os": {
                    "aws_region": loaded_metadata.aws_default_region,
                    "function_name": loaded_metadata.aws_lambda_function_name,
                }
            }
        except Exception:
            self_logger.exception(
                "Exception(Logger): Failed to extract Lambda environment variables"
            )
            return None
        else:
            return response
