import logging
import os
import uuid
from typing import TYPE_CHECKING, Any

import structlog

from i_dot_ai_utilities.logging.enrichers.enrichment_provider import (
    EnrichmentProvider,
    ExecutionEnvironmentType,
)
from i_dot_ai_utilities.logging.processor_helper import ProcessorHelper
from i_dot_ai_utilities.logging.types.context_enrichment_options import (
    ContextEnrichmentOptions,
)
from i_dot_ai_utilities.logging.types.context_fields import ContextFieldValue
from i_dot_ai_utilities.logging.types.fargate_enrichment_schema import (
    ExtractedFargateContext,
)
from i_dot_ai_utilities.logging.types.log_output_format import LogOutputFormat
from i_dot_ai_utilities.logging.types.logger_config_options import LoggerConfigOptions

if TYPE_CHECKING:
    from i_dot_ai_utilities.logging.types.base_context import BaseContext
    from i_dot_ai_utilities.logging.types.fastapi_enrichment_schema import (
        ExtractedFastApiContext,
    )


class StructuredLogger:
    """Create a new Structured Logger.

    Logs can be output in either JSON or Console format. The execution environment can also be set to enrich log messages with environment-specific context.

    See the i.AI utils readme for full details on usage.

    :param level: The logging level. Log messages raised at this urgency or above will be written to stdout. Defaults to 'INFO'. Example values: 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR'
    :param options: A set of LoggerConfigOptions that can be used to modify the logger behaviour.
    """  # noqa: E501

    _logger: Any
    _default_config: LoggerConfigOptions
    _execution_environment: ExecutionEnvironmentType
    _enricher_provider: EnrichmentProvider
    _log_format: LogOutputFormat
    _ship_logs: bool
    _logger_name: str | None = None

    def __init__(
        self,
        level: str | int = logging.INFO,
        options: LoggerConfigOptions | None = None,
    ):
        self._default_config = self._load_config_defaults()
        if not options:
            options = self._default_config

        self._logger = structlog.get_logger()

        self._logger_name = options.get("logger_name", None)

        self._execution_environment = options.get(
            "execution_environment", self._default_config["execution_environment"]
        )
        self._enricher_provider = EnrichmentProvider(self._execution_environment)

        self._log_format = options.get("log_format", self._default_config["log_format"])
        self._ship_logs = self._should_ship_logs(options)

        ProcessorHelper().configure_processors(
            self._normalise_log_level(level),
            self._log_format,
        )

        self._upsert_base_context()

    def debug(self, message_template: str, **kwargs: Any) -> None:
        """Write a debug log message.

        Log messages may be string-literals, or be formatted strings. Formatted strings will have any interpolated values added as context fields to the log message.

        Usage Examples
        ----------
        logger.debug('User started login flow', id=12345) # Log output: {"message": "User started login flow", "id": 12345}

        logger.debug('User {email} has logged in', email='me@example.com') # Log output: {"message": "User me@example.com has logged in", "email": "me@example.com"}

        :param message_template: The string literal or formatted string to pass to the logger.
        :param **kwargs: Arguments passed to interpolate into a formatted string, if using.
        """  # noqa: E501
        message = self._get_interpolated_message(message_template, **kwargs)
        self._logger.debug(message, message_template=message_template, **kwargs)

    def info(self, message_template: str, **kwargs: Any) -> None:
        """Write an informational log message.

        Log messages may be string-literals, or be formatted strings. Formatted strings will have any interpolated values added as context fields to the log message.

        Usage Examples
        ----------
        logger.info('User started login flow', id=12345) # Log output: {"message": "User started login flow", "id": 12345}

        logger.info('User {email} has logged in', email='me@example.com') # Log output: {"message": "User me@example.com has logged in", "email": "me@example.com"}

        :param message_template: The string literal or formatted string to pass to the logger.
        :param **kwargs: Arguments passed to interpolate into a formatted string, if using.
        """  # noqa: E501
        message = self._get_interpolated_message(message_template, **kwargs)
        self._logger.info(message, message_template=message_template, **kwargs)

    def warning(self, message_template: str, **kwargs: Any) -> None:
        """Write a warning log message.

        Log messages may be string-literals, or be formatted strings. Formatted strings will have any interpolated values added as context fields to the log message.

        Usage Examples
        ----------
        logger.warning('anonymous user failed to do X action', id=12345)
        => Log output: {"message": "anonymous user failed to do X action", "id": 12345}

        logger.warning('User {email} failed to log in due to password mismatch', email='me@example.com')
        => Log output: {"message": "User me@example.com failed to log in due to password mismatch", "email": "me@example.com"}

        :param message_template: The string literal or formatted string to pass to the logger.
        :param **kwargs: Arguments passed to interpolate into a formatted string, if using.
        """  # noqa: E501
        message = self._get_interpolated_message(message_template, **kwargs)
        self._logger.warning(message, message_template=message_template, **kwargs)

    def error(self, message_template: str, **kwargs: Any) -> None:
        """Write an error log message.

        Log messages may be string-literals, or be formatted strings. Formatted strings will have any interpolated values added as context fields to the log message.

        Usage Examples
        ----------
        logger.error('An error occurred with user login flow', id=12345) # Log output: {"message": "User started login flow", "id": 12345}

        logger.error('Logout failed for user {email}', email='me@example.com') # Log output: {"message": "Logout failed for user me@example.com", "email": "me@example.com"}

        :param message_template: The string literal or formatted string to pass to the logger.
        :param **kwargs: Arguments passed to interpolate into a formatted string, if using.
        """  # noqa: E501
        message = self._get_interpolated_message(message_template, **kwargs)
        self._logger.error(message, message_template=message_template, **kwargs)

    def exception(self, message_template: str, **kwargs: Any) -> None:
        """Write a caught exception, along with an error log message. Caught exceptions will automatically be added as context to the log message.

        Log messages may be string-literals, or be formatted strings. Formatted strings will have any interpolated values added as context fields to the log message.

        Usage Examples
        ----------
        logger.exception('Login flow failed for user', id=12345) # Log output: {"message": "Login flow failed for user", "id": 12345, "exception": "Traceback ..."}

        logger.exception('User {email} failed to update in DB', email='me@example.com') # Log output: {"message": User me@example.com failed to update in DB", "email": "me@example.com", "exception": "Traceback ..."}
        """  # noqa: E501
        message = self._get_interpolated_message(message_template, **kwargs)
        self._logger.exception(message, message_template=message_template, **kwargs)

    def set_context_field(self, field_key: str, field_value: ContextFieldValue) -> None:
        """Add a custom field to the logger dictionary. This field will appear on subsequent log messages.

        This key and value will be made available as a searchable field in the downstream logging stack.

        A field set using this function will be removed upon a refresh of the logger context.

        :param field_key: The key of the field.
        :param field_value: The value of the field.
        """  # noqa: E501
        structlog.contextvars.bind_contextvars(**{field_key: field_value})

    def refresh_context(
        self, context_enrichers: list[ContextEnrichmentOptions] | None = None
    ) -> None:
        """Reset the logger, creating a new context id and removing any custom fields set since the previous invocation.

        :param context_enrichers: A list of one or more ContextEnrichmentOptions. Used to refresh the new logger with fields from well-known frameworks, such as FastAPI request metadata.
        """  # noqa: E501
        structlog.contextvars.clear_contextvars()
        self._upsert_base_context()

        if context_enrichers is None:
            return

        additional_context: ExtractedFastApiContext | dict[str, Any] = {}
        for enricher in context_enrichers:
            enricher_type = enricher["type"]
            enricher_object = enricher["object"]
            ctx = self._enricher_provider.extract_context_from_framework_enricher(
                self, enricher_type, enricher_object
            )

            if ctx is not None:
                additional_context.update(ctx)

        structlog.contextvars.bind_contextvars(**additional_context)

    def _should_ship_logs(self, options: LoggerConfigOptions) -> bool:
        selected_option = options.get("ship_logs", self._default_config["ship_logs"])
        if (
            options.get("log_format", self._default_config["log_format"])
            is not LogOutputFormat.JSON
            and selected_option is True
        ):
            self._logger.warning(
                "Warning(Logger): messages cannot be shipped downstream "
                "outside of JSON format. Disabling log shipping"
            )
            return False

        return selected_option

    def _get_interpolated_message(self, message_template: str, **kwargs: Any) -> str:
        try:
            return message_template.format(**kwargs)
        except KeyError:
            self._logger.exception(
                (
                    "Exception(Logger): Variable interpolation failed when formatting "
                    "log message. Is a value missing?"
                ),
                message_template=message_template,
            )
            return message_template

    def _set_environment_context(
        self, environment_context: ExtractedFargateContext | None
    ) -> None:
        if environment_context:
            structlog.contextvars.bind_contextvars(**environment_context)

    def _set_logger_name(self) -> None:
        if self._logger_name:
            structlog.contextvars.bind_contextvars(
                logger_name=self._logger_name,
            )

    def _upsert_base_context(self) -> None:
        self._set_environment_context(
            self._enricher_provider.load_execution_environment_context(self)
        )

        self._set_logger_name()

        base_context: BaseContext = {
            "context_id": str(uuid.uuid4()),
            "env_app_name": os.environ.get("APP_NAME", "unknown"),
            "env_repo_name": os.environ.get("REPO", "unknown"),
            "env_environment_name": os.environ.get("ENVIRONMENT", "unknown"),
            "ship_logs": 1 if self._ship_logs else 0,
        }
        structlog.contextvars.bind_contextvars(**base_context)

    def _normalise_log_level(self, level: str | int) -> int:
        match level.upper() if isinstance(level, str) else level:
            case "DEBUG" | logging.DEBUG:
                return logging.DEBUG
            case "INFO" | "INFORMATION" | "INFORMATIONAL" | logging.INFO:
                return logging.INFO
            case "WARN" | "WARNING" | logging.WARNING:
                return logging.WARNING
            case "ERROR" | logging.ERROR:
                return logging.ERROR
            case _:
                self._logger.warning(
                    "Log level {level} not recognised, defaulting to INFO", level=level
                )
                return logging.INFO

    def _load_config_defaults(self) -> LoggerConfigOptions:
        return {
            "execution_environment": ExecutionEnvironmentType.FARGATE,
            "log_format": LogOutputFormat.JSON,
            "ship_logs": True,
        }
