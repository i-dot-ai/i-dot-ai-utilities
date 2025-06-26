from typing import NotRequired, TypedDict

from i_dot_ai_utilities.logging.enrichers.enrichment_provider import (
    ExecutionEnvironmentType,
)
from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat


class LoggerConfigOptions(TypedDict):
    execution_environment: NotRequired[ExecutionEnvironmentType]
    log_format: NotRequired[LogOutputFormat]
    ship_logs: NotRequired[bool]
    logger_name: NotRequired[str]
