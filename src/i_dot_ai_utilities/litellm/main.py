from typing import Any

import litellm
import requests
from litellm import check_valid_key, completion, embedding
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from litellm.llms.openai.common_utils import OpenAIError
from litellm.types.utils import EmbeddingResponse, ModelResponse
from requests import RequestException

from i_dot_ai_utilities.litellm.exceptions import MiscellaneousLiteLLMError, ModelNotAvailableError
from i_dot_ai_utilities.litellm.settings import Settings
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger

settings = Settings()


def _check_model_is_callable(model: str, logger: StructuredLogger | None = None) -> bool:
    result = check_valid_key(model, litellm.api_key)
    if not result and logger:
        logger.error("Model {model} is not available on key {api_key}", model=model, api_key=litellm.api_key[:6])
    elif result and logger:
        logger.debug("Model {model} available on key {api_key}", model=model, api_key=litellm.api_key[:6])
    return result  # type: ignore[no-any-return]


class LiteLLMHandler:
    def __init__(self, logger: StructuredLogger) -> None:
        self.logger = logger
        self._configure_litellm()

    def _configure_litellm(self) -> None:
        if settings.api_key:
            litellm.api_key = settings.api_key
        if settings.api_base:
            litellm.api_base = settings.api_base
        if settings.api_version:
            litellm.api_version = settings.api_version
        if settings.organisation:
            litellm.organization = settings.organisation
        litellm.request_timeout = settings.timeout

        response = check_valid_key(settings.model, litellm.api_key)
        if not response:
            #  Slicing API key to not expose entire key in logs
            self.logger.error(
                "Invalid API key {api_key} for model {model}", api_key=litellm.api_key[:6], model=settings.model
            )
        if settings.langfuse_public_key and settings.langfuse_secret_key:
            self.logger.info("Langfuse callback configured by environment variables")
            litellm.success_callback = ["langfuse"]
        try:
            response = requests.get(settings.api_base, timeout=60)
            response.raise_for_status()
            self.logger.info("LiteLLM configured and reachable on {api_base}", api_base=settings.api_base)
        except (RequestException, requests.HTTPError):
            self.logger.exception("Failed to connect to API")

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        should_stream: bool = False,
        **kwargs: dict[str, Any],
    ) -> ModelResponse | CustomStreamWrapper:
        """
        A function that calls chat completion within LiteLLM
        :param messages: The messages to send to the LLM
        :param model: The model name
        :param temperature: The temperature to use
        :param max_tokens: The maximum number of tokens to use
        :param should_stream: Whether to stream the response, defaults to `False`
        :param kwargs: The keyword arguments to pass to the LiteLLM API
        :return: The response from the chat as either a ModelResponse or CustomStreamWrapper,
        depending on whether `should_stream` is used
        :raises ModelNotAvailableException: occurs when the given or default model is not available on the given key
        :raises MiscellaneousLiteLLMException: occurs when the called method in the
        litellm sdk returns a generic openai exception
        """
        if (model and not _check_model_is_callable(model)) and not _check_model_is_callable(settings.model):
            raise ModelNotAvailableError("The given model is not available on this api key", 401)
        try:
            return completion(
                model=model or settings.model,
                messages=messages,
                temperature=temperature or settings.temperature,
                max_tokens=max_tokens or settings.max_tokens,
                stream=should_stream,
                **kwargs,
            )
        except OpenAIError as e:
            self.logger.exception("Failed to get chat completion")
            raise MiscellaneousLiteLLMError(
                "Failed to get chat completion, generic error occurred inside LiteLLM", 500
            ) from e

    async def get_embedding(self, text: str, model: str | None = None, **kwargs: dict[str, Any]) -> EmbeddingResponse:
        """
        Async method for embedding given text
        :param text: The text to embed
        :param model: The model to use for embedding, or defaults to environment variable model
        :param kwargs: Any kwargs to pass to the embedding call
        :return: `EmbeddingResponse` object
        :raises ModelNotAvailableException: occurs when the given or default model is not available on the given key
        :raises MiscellaneousLiteLLMException: occurs when the called method in the
        litellm sdk returns a generic openai exception
        """
        if (model and not _check_model_is_callable(model)) and not _check_model_is_callable(settings.model):
            raise ModelNotAvailableError("The given model is not available on this api key", 401)
        try:
            response: EmbeddingResponse = await embedding(model=model or settings.model, input=text, **kwargs)
        except OpenAIError as e:
            self.logger.exception("Failed to get embedding")
            raise MiscellaneousLiteLLMError(
                "Failed to get embedding, generic error occurred inside LiteLLM", 500
            ) from e
        else:
            return response

    @staticmethod
    def get_available_models() -> list[str]:
        """
        Returns a list of available models
        :return: list[str] The models available
        """
        return litellm.model_list  # type: ignore[no-any-return]
