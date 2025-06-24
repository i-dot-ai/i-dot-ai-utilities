from typing import NotRequired, TypedDict

from i_dot_ai_utilities.logging.enrichers.enrichment_provider import (
    ExecutionEnvironmentType,
)
from i_dot_ai_utilities.logging.types import log_output_format


class LoggerConfigOptions(TypedDict):
    execution_environment: NotRequired[ExecutionEnvironmentType]
    log_format: NotRequired[log_output_format]
    ship_logs: NotRequired[bool]
