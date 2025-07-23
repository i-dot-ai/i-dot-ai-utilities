from typing import Any

import litellm
from litellm import check_valid_key, completion, embedding
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from litellm.llms.openai.common_utils import OpenAIError
from litellm.types.utils import ModelResponse

from i_dot_ai_utilities.litellm.settings import Settings
from i_dot_ai_utilities.logging.structured_logger import StructuredLogger

settings = Settings()


def _check_model_is_callable(model: str) -> bool:
    return check_valid_key(model, litellm.api_key)  # type: ignore[no-any-return]


class LiteLLMHandler:
    def __init__(self, logger: StructuredLogger) -> None:
        self._configure_litellm()
        self.logger = logger

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
            self.logger.error(
                "Invalid API key {api_key} for model {model}", api_key=litellm.api_key, model=settings.model
            )
        if settings.langfuse_public_key and settings.langfuse_secret_key:
            litellm.success_callback = ["langfuse"]

    def chat_completion(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: dict[str, Any],
    ) -> ModelResponse | CustomStreamWrapper | None:
        """Generate chat completion using LiteLLM"""
        if (model and not _check_model_is_callable(model)) and _check_model_is_callable(settings.model):
            self.logger.error(
                "The given and default model are not supported for the configured key: Given model {model}", model=model
            )
        try:
            return completion(
                model=model or settings.model,
                messages=messages,
                temperature=temperature or settings.temperature,
                max_tokens=max_tokens or settings.max_tokens,
                **kwargs,
            )
        except OpenAIError:
            self.logger.exception("Failed to get chat completion")
            return None

    def get_embedding(self, text: str, model: str | None = None, **kwargs: dict[str, Any]) -> list[float]:
        """Get text embedding using LiteLLM"""
        response = embedding(model=model or "text-embedding-ada-002", input=text, **kwargs)
        return response["data"][0]["embedding"]  # type: ignore[no-any-return]

    def stream_completion(
        self,
        messages: list[dict[str, str]],
        model: str | None = None,
        temperature: float | None = None,
        **kwargs: dict[str, Any],
    ) -> ModelResponse | CustomStreamWrapper | None:
        """Stream chat completion using LiteLLM"""
        return completion(
            model=model or settings.model,
            messages=messages,
            temperature=temperature or settings.temperature,
            stream=True,
            **kwargs,
        )

    def set_model(self, model: str) -> None:
        """Update the default model"""
        settings.model = model

    def get_available_models(self) -> list[str]:
        """Get list of available models"""
        return litellm.model_list  # type: ignore[no-any-return]
