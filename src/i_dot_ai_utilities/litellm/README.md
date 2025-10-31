# LiteLLM

## Usage

### Create a LiteLLMHandler object

You can create a `LiteLLMHandler` object very easily by instantiating an instance of the `LiteLLMHandler` class:
```python
from i_dot_ai_utilities.litellm import LiteLLMHandler

# `logger` should be a `StructuredLogger` object from the `logging` module
litellm_handler = LiteLLMHandler(logger)

litellm_handler.chat_completion([{
    "role": "user",
    "content": "Hello!"
}])
```
This is enough to start a chat with the LLM via LLMGateway.

<br>

***

<br>

This package takes configuration from your environment variables using pydantic-settings. The `IAI_LITELLM__` prefix is used to allow you to keep settings for other tools separately.

Please set the following settings:

- `IAI_LITELLM_CHAT_MODEL: str`: The name of the model you'd like to use
- `IAI_LITELLM_EMBEDDING_MODEL: str`: The name of the model you'd like to use
- `IAI_LITELLM_API_KEY: str`: The API key for LiteLLM
- `IAI_LITELLM_API_BASE: str`: The URL for LiteLLM including protocol
- `IAI_LITELLM_API_VERSION: str`: (Optional) The API version to use
- `IAI_LITELLM_ORGANISATION: str`: (Optional) The organisation in LiteLLM your key belongs to
- `IAI_LITELLM_TEMPERATURE: float`: (Optional) The temperature to use in model calls (this can be overridden in the function calls)
- `IAI_LITELLM_MAX_TOKENS: int`: (Optional) The max tokens to use when calling LiteLLM (this can be overridden in the function calls)
- `IAI_LITELLM_MAX_TIMEOUT: int`: (Optional) The timeout for calls to LiteLLM
- `IAI_LITELLM_LANGFUSE_PUBLIC_KEY: str`: (Optional) The public key to connect LiteLLM callbacks to langfuse
- `IAI_LITELLM_LANGFUSE_SECRET_KEY: str`: (Optional) The secret key to connect LiteLLM callbacks to langfuse
- `IAI_LITELLM_LANGFUSE_HOST: str`: (Optional) The custom host to connect LiteLLM callbacks to langfuse

<br>

***

<br>

### Important notes

> Note that the functions will return custom exceptions for most errors that occur within the package.
> This is due to the underlying LiteLLM SDK returning most errors as generic `OpenAIError` exceptions


> When using Gemini models, e.g. `gemini-2.5-flash`, please use the openrouter provider when calling the models.
> e.g. `openrouter/gemini-2.5-flash`, and not `vertex_ai/gemini-2.5-flash` or `gemini/gemini-2.5-flash`
<br>

***

<br>

### Supported functionality
Once the litellm handler is initialised, you have three currently supported methods available for use.
The following three methods are available:

#### Chat completion

``` python
litellm_handler.chat_completion([{
    "role": "user",
    "content": "Hello!"
}])
```

#### Embedding

``` python
await litellm_handler.get_embedding("test file content as string")
```

#### Get available models

``` python
model_list = litellm_handler.get_available_models()
```
