from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    chat_model: str = Field(default="o4-mini", description="Default model to use for chat completions")
    embedding_model: str = Field(
        default="text-embedding-3-small", description="Default model to use for text embedding"
    )
    api_key: str = Field(description="API key for the service")
    api_base: str = Field(description="Custom API base URL")
    project_name: str = Field(description="Project name for carbon tracking, set to the app name or repo name")
    api_version: str | None = Field(default=None, description="Custom API base URL")
    organisation: str | None = Field(default=None, description="LiteLLM organisation")
    temperature: float = Field(default=1.0, ge=0.0, le=2.0, description="Temperature for generation")
    max_tokens: int | None = Field(default=None, description="Maximum tokens to generate")
    timeout: int = Field(default=30, description="Request timeout in seconds")
    langfuse_public_key: str | None = Field(default=None, description="Public key for langfuse")
    langfuse_secret_key: str | None = Field(default=None, description="Secret key for langfuse")
    langfuse_host: str | None = Field(default=None, description="Custom host for langfuse callback")

    model_config = SettingsConfigDict(extra="ignore", env_prefix="IAI_LITELLM_", case_sensitive=False)
