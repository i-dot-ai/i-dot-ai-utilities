from collections.abc import Callable

import structlog
from structlog.dev import ConsoleRenderer
from structlog.processors import JSONRenderer

from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat


class ProcessorHelper:
    def configure_processors(self, log_level: int, log_format: LogOutputFormat) -> None:
        structlog.configure(
            wrapper_class=structlog.make_filtering_bound_logger(log_level)
        )
        structlog.configure(
            processors=[
                structlog.contextvars.merge_contextvars,
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.add_log_level,
                structlog.processors.EventRenamer("message"),
                *self._load_output_processors(log_format),
            ]
        )

    def _load_output_processors(
        self, output_type: LogOutputFormat
    ) -> tuple[Callable, ...]:
        match output_type:
            case LogOutputFormat.JSON:
                return (
                    structlog.processors.format_exc_info,
                    JSONRenderer(),
                )
            case _:
                return (ConsoleRenderer(),)
