import pytest
from ecologits.tracers.litellm_tracer import ChatCompletion
from litellm.types.utils import Choices, EmbeddingResponse, ModelResponse

from i_dot_ai_utilities.litellm.exceptions import MiscellaneousLiteLLMError, ModelNotAvailableError
from i_dot_ai_utilities.litellm.main import LiteLLMHandler, settings


def test_chat(litellm_client: LiteLLMHandler) -> None:
    result: ModelResponse = litellm_client.chat_completion(
        [{"role": "user", "content": "This is a test, please reply with just the word 'Hello' if you are available"}]
    )
    assert result
    assert isinstance(result, ChatCompletion)
    assert result.choices
    choice: Choices = result.choices[0]  # type: ignore[assignment]
    assert choice.message.content == "Hello"


def test_invalid_model(litellm_client: LiteLLMHandler) -> None:
    with pytest.raises(ModelNotAvailableError):
        litellm_client.chat_completion(
            [
                {
                    "role": "user",
                    "content": "This is a test, please reply with just the word 'Hello' if you are available",
                }
            ],
            model="fake-model",
        )


def test_chat_stream(litellm_client: LiteLLMHandler) -> None:
    response_content = ""
    for chunk in litellm_client.chat_completion_stream(
        messages=[
            {"role": "user", "content": "This is a test, please reply with just the word 'Hello' if you are available"}
        ],
    ):
        if hasattr(chunk, "choices") and chunk.choices:
            delta = chunk.choices[0].delta
            if delta["content"]:
                content = delta["content"]
                response_content += content
    assert response_content == "Hello"


def test_default_models_exists_in_model_list(litellm_client: LiteLLMHandler) -> None:
    result = litellm_client.get_all_models()
    assert result
    assert settings.chat_model in result
    assert settings.embedding_model in result


def test_assert_fake_not_in_model_list(litellm_client: LiteLLMHandler) -> None:
    result = litellm_client.get_all_models()
    assert result
    assert "fake-model" not in result


def test_get_embedding(litellm_client: LiteLLMHandler) -> None:
    result = litellm_client.get_embedding("This is a test text for embedding", model="text-embedding-3-small")

    assert result
    assert isinstance(result, EmbeddingResponse)
    assert result.data
    assert len(result.data) > 0
    assert result.data[0]["embedding"]
    assert len(result.data[0]["embedding"]) > 0  # Should have embedding vector


def test_get_embedding_invalid_model(litellm_client: LiteLLMHandler) -> None:
    with pytest.raises(MiscellaneousLiteLLMError):
        litellm_client.get_embedding("This is a test text for embedding", model="fake-embedding-model")
