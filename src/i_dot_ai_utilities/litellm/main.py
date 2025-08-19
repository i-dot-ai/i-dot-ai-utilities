from typing import Any

import litellm
import requests
from ecologits import EcoLogits
from litellm import BadRequestError, check_valid_key
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from litellm.llms.openai.common_utils import OpenAIError
from litellm.types.utils import EmbeddingResponse, ModelResponse
from requests import RequestException

from i_dot_ai_utilities.litellm.exceptions import MiscellaneousLiteLLMError, ModelNotAvailableError
from i_dot_ai_utilities.litellm.settings import Settings
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger

settings = Settings()


def _check_chat_model_is_callable(model: str, logger: StructuredLogger | None = None) -> bool:
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
        self.chat_model = settings.chat_model
        self.embedding_model = settings.embedding_model
        litellm.request_timeout = settings.timeout

        response = _check_chat_model_is_callable(self.chat_model, self.logger)
        if not response:
            #  Slicing API key to not expose entire key in logs
            self.logger.error(
                "Invalid API key {api_key} for model {model}", api_key=litellm.api_key[:6], model=self.chat_model
            )
        if settings.langfuse_public_key and settings.langfuse_secret_key:
            self.logger.info("Langfuse callback configured by environment variables")
            litellm.success_callback = ["langfuse"]
        try:
            response = requests.get(settings.api_base, timeout=60)
            response.raise_for_status()
            self.logger.info("LiteLLM configured and reachable on {api_base}", api_base=settings.api_base)
            EcoLogits.init(providers=["litellm"])
            self.logger.info("Ecologits added for litellm, using WOR energy zone")
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
        try:
            if model and not _check_chat_model_is_callable(model, self.logger):
                raise ModelNotAvailableError("The given model is not available on this api key", 401)
            if not model and not _check_chat_model_is_callable(self.chat_model, self.logger):
                raise ModelNotAvailableError("The default model is not available on this api key", 401)

            response = litellm.completion(
                model=model or self.chat_model,
                messages=messages,
                temperature=temperature or settings.temperature,
                max_tokens=max_tokens or settings.max_tokens,
                stream=should_stream,
                stream_options={"include_usage": True} if should_stream else None,
                **kwargs,
            )

            if response.impacts:
                self.logger.info(
                    "Carbon cost for completion call: Electricity total {electricity_unit}: "
                    "{electricity_value_min} to {electricity_value_max}. "
                    "Global warming potential {gwp_unit}: {gwp_value_min} to {gwp_value_max}. "
                    "Abiotic resource depletion {adpe_unit}: {adpe_value_min} to {adpe_value_max}. "
                    "Primary source energy used {pe_unit}: {pe_value_min} to {pe_value_max}.",
                    electricity_unit=response.impacts.energy.unit,
                    electricity_value_min=response.impacts.energy.value.min,
                    electricity_value_max=response.impacts.energy.value.max,
                    gwp_unit=response.impacts.gwp.unit,
                    gwp_value_min=response.impacts.gwp.value.min,
                    gwp_value_max=response.impacts.gwp.value.max,
                    adpe_unit=response.impacts.adpe.unit,
                    adpe_value_min=response.impacts.adpe.value.min,
                    adpe_value_max=response.impacts.adpe.value.max,
                    pe_unit=response.impacts.pe.unit,
                    pe_value_min=response.impacts.pe.value.min,
                    pe_value_max=response.impacts.pe.value.max,
                )
            self.logger.info(
                "Chat completion called for model {model}, with {number_of_messages} messages",
                model=model or self.chat_model,
                number_of_messages=len(messages),
            )
        except BadRequestError as e:
            self.logger.exception("Failed to get chat completion")
            raise MiscellaneousLiteLLMError(str(e), 400) from e
        except OpenAIError as e:
            self.logger.exception("Failed to get chat completion")
            raise MiscellaneousLiteLLMError(str(e), 500) from e

        return response

    def get_embedding(self, text: str, model: str | None = None, **kwargs: dict[str, Any]) -> EmbeddingResponse:
        """
        Method for embedding given text with carbon tracking
        :param text: The text to embed
        :param model: The model to use for embedding, or defaults to environment variable model
        :param kwargs: Any kwargs to pass to the embedding call
        :return: `EmbeddingResponse` object
        :raises ModelNotAvailableException: occurs when the given or default model is not available on the given key
        :raises MiscellaneousLiteLLMException: occurs when the called method in the
        litellm sdk returns a generic openai exception
        """
        try:
            # LiteLLM doesn't support any way to pre-validate an embedding model
            response = litellm.embedding(model=model or self.embedding_model, input=text, **kwargs)
        except BadRequestError as e:
            self.logger.exception("Failed to get embedding")
            raise MiscellaneousLiteLLMError(str(e), 400) from e
        except OpenAIError as e:
            self.logger.exception("Failed to get embedding")
            raise MiscellaneousLiteLLMError(str(e), 500) from e
        else:
            return response

    @staticmethod
    def get_all_models() -> list[str]:
        """
        Returns a list of available models
        :return: list[str] The models available
        """
        return litellm.model_list  # type: ignore[no-any-return]
