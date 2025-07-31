import time
from typing import Any

import litellm
import requests
from codecarbon import EmissionsTracker
from codecarbon.core.units import Energy
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
        except (RequestException, requests.HTTPError):
            self.logger.exception("Failed to connect to API")

    def __log_carbon_info(self, message: str, carbon_info: dict[str, float | None | Energy]):
        self.logger.info(
            "{message}"
            "Emissions CO2 kg: {emissions_kg_co2}. "
            "Duration seconds: {duration_seconds}. "
            "Electricity total kWh: {energy_consumed}. "
            "CPU energy kwh: {cpu_energy_kwh}. "
            "GPU energy kwh: {gpu_energy_kwh}. "
            "RAM energy kwh: {ram_energy_kwh}. "
            "CPU power watts: {cpu_power_watts}. "
            "GPU power watts: {gpu_power_watts}. "
            "RAM power watts: {ram_power_watts}. "
            "Carbon intensity g/kwh: {carbon_intensity_g_per_kwh}. "
            "Country name: {country_name}. "
            "Region: {region}. "
            "Model used: {model_used}. "
            "Text length: {text_length}. "
            "Token estimate: {text_token_estimate}. "
            "Actual token usage: {actual_token_usage}.",
            message=message,
            emissions_kg_co2=carbon_info["emissions_kg_co2"],
            duration_seconds=carbon_info["duration_seconds"],
            energy_consumed=carbon_info["energy_consumed_kwh"].kWh,
            cpu_energy_kwh=carbon_info["cpu_energy_kwh"],
            gpu_energy_kwh=carbon_info["gpu_energy_kwh"],
            ram_energy_kwh=carbon_info["ram_energy_kwh"],
            cpu_power_watts=carbon_info["cpu_power_watts"],
            gpu_power_watts=carbon_info["gpu_power_watts"],
            ram_power_watts=carbon_info["ram_power_watts"],
            carbon_intensity_g_per_kwh=carbon_info["carbon_intensity_g_per_kwh"],
            country_name=carbon_info["country_name"],
            region=carbon_info["region"],
            model_used=carbon_info["model_used"],
            text_length=carbon_info["text_length"],
            text_token_estimate=carbon_info["text_token_estimate"],
            actual_token_usage=carbon_info["actual_token_usage"],
        )

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
        tracker = EmissionsTracker(
            project_name=settings.project_name,
            measure_power_secs=1,
            save_to_file=False,
            logging_logger=None,
        )

        start_time = time.perf_counter()
        tracker.start()

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
            self.logger.info(
                "Chat completion called for model {model}, with {number_of_messages} messages",
                model=model or self.chat_model,
                number_of_messages=len(messages),
            )
        except BadRequestError as e:
            tracker.stop()
            self.logger.exception("Failed to get chat completion")
            raise MiscellaneousLiteLLMError(str(e), 400) from e
        except OpenAIError as e:
            tracker.stop()
            self.logger.exception("Failed to get chat completion")
            raise MiscellaneousLiteLLMError(str(e), 500) from e
        else:
            emissions = tracker.stop()
            end_time = time.perf_counter()

        if should_stream:
            #  Iterate chunks to force usage to update on final chunk
            for _ in response:
                pass
            actual_token_usage = response.chunks[-1].usage
        else:
            actual_token_usage = response.usage.total_tokens

        carbon_info = {
            "emissions_kg_co2": emissions,
            "duration_seconds": end_time - start_time,
            "energy_consumed_kwh": getattr(tracker, "_total_energy", None),
            "cpu_energy_kwh": getattr(tracker, "_cpu_energy", None),
            "gpu_energy_kwh": getattr(tracker, "_gpu_energy", None),
            "ram_energy_kwh": getattr(tracker, "_ram_energy", None),
            "cpu_power_watts": getattr(tracker, "_last_measured_cpu_power", None),
            "gpu_power_watts": getattr(tracker, "_last_measured_gpu_power", None),
            "ram_power_watts": getattr(tracker, "_last_measured_ram_power", None),
            "carbon_intensity_g_per_kwh": getattr(tracker, "_carbon_intensity", None),
            "country_name": getattr(tracker, "_country_name", None),
            "region": getattr(tracker, "_region", None),
            "model_used": model or self.chat_model,
            "text_length": sum(len(message["content"]) for message in messages) or 0,
            "text_token_estimate": None,  # Estimating chat is far more complex than embedding, so leave empty
            "actual_token_usage": actual_token_usage,
        }

        self.__log_carbon_info("Carbon cost for chat completion call: ", carbon_info)

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

        # Tracker auto-detects region so we need to keep an eye on this being accurate
        tracker = EmissionsTracker(
            project_name=settings.project_name,
            measure_power_secs=1,
            save_to_file=False,
            logging_logger=None,
        )

        start_time = time.perf_counter()
        tracker.start()

        try:
            # LiteLLM doesn't support any way to pre-validate an embedding model
            response = litellm.embedding(model=model or self.embedding_model, input=text, **kwargs)
        except BadRequestError as e:
            tracker.stop()
            self.logger.exception("Failed to get embedding")
            raise MiscellaneousLiteLLMError(str(e), 400) from e
        except OpenAIError as e:
            tracker.stop()
            self.logger.exception("Failed to get embedding")
            raise MiscellaneousLiteLLMError(str(e), 500) from e
        else:
            emissions = tracker.stop()
            end_time = time.perf_counter()

            carbon_info = {
                "emissions_kg_co2": emissions,
                "duration_seconds": end_time - start_time,
                "energy_consumed_kwh": getattr(tracker, "_total_energy", None),
                "cpu_energy_kwh": getattr(tracker, "_cpu_energy", None),
                "gpu_energy_kwh": getattr(tracker, "_gpu_energy", None),
                "ram_energy_kwh": getattr(tracker, "_ram_energy", None),
                "cpu_power_watts": getattr(tracker, "_last_measured_cpu_power", None),
                "gpu_power_watts": getattr(tracker, "_last_measured_gpu_power", None),
                "ram_power_watts": getattr(tracker, "_last_measured_ram_power", None),
                "carbon_intensity_g_per_kwh": getattr(tracker, "_carbon_intensity", None),
                "country_name": getattr(tracker, "_country_name", None),
                "region": getattr(tracker, "_region", None),
                "model_used": model or self.embedding_model,
                "text_length": len(text),
                "text_token_estimate": len(text.split()),
                "actual_token_usage": response.usage.total_tokens,
            }

            self.__log_carbon_info("Carbon cost for embedding call: ", carbon_info)

            return response

    @staticmethod
    def get_all_models() -> list[str]:
        """
        Returns a list of available models
        :return: list[str] The models available
        """
        return litellm.model_list  # type: ignore[no-any-return]
