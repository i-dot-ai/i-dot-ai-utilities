import pytest
from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from litellm.types.utils import EmbeddingResponse, ModelResponse

from i_dot_ai_utilities.litellm.exceptions import MiscellaneousLiteLLMError, ModelNotAvailableError
from i_dot_ai_utilities.litellm.main import settings


def test_chat(litellm_client):
    result = litellm_client.chat_completion(
        [{"role": "user", "content": "This is a test, please reply with just the word 'Hello' if you are available"}]
    )
    assert result
    assert type(result) is ModelResponse
    assert result.choices[0].message.content == "Hello"


def test_invalid_model(litellm_client):
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


def test_chat_stream(litellm_client):
    result = litellm_client.chat_completion(
        [{"role": "user", "content": "This is a test, please reply with just the word 'Hello' if you are available"}],
        should_stream=True,
    )
    assert result
    assert type(result) is CustomStreamWrapper
    assert result.completion_stream.model_response.choices[0].message.content == "Hello"


def test_default_models_exists_in_model_list(litellm_client):
    result = litellm_client.get_all_models()
    assert result
    assert settings.chat_model in result
    assert settings.embedding_model in result


def test_assert_fake_not_in_model_list(litellm_client):
    result = litellm_client.get_all_models()
    assert result
    assert "fake-model" not in result


def test_get_embedding(litellm_client):
    result = litellm_client.get_embedding("This is a test text for embedding", model="text-embedding-3-small")

    assert result
    assert type(result) is EmbeddingResponse
    assert result.data
    assert len(result.data) > 0
    assert result.data[0]["embedding"]
    assert len(result.data[0]["embedding"]) > 0  # Should have embedding vector


def test_get_embedding_invalid_model(litellm_client):
    with pytest.raises(MiscellaneousLiteLLMError):
        litellm_client.get_embedding("This is a test text for embedding", model="fake-embedding-model")
