from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kurumi_proxy_api_key: str | None = Field(default=None, alias="KURUMI_PROXY_API_KEY")
    codebuddy_api_key: str | None = Field(default=None, alias="CODEBUDDY_API_KEY")
    codebuddy_bin: str = Field(default="codebuddy", alias="CODEBUDDY_BIN")
    codebuddy_model: str = Field(default="default-model", alias="CODEBUDDY_MODEL")
    codebuddy_timeout_seconds: float = Field(default=180, alias="CODEBUDDY_TIMEOUT_SECONDS", gt=0)
    kurumi_proxy_max_output_tokens: int = Field(
        default=8192,
        alias="KURUMI_PROXY_MAX_OUTPUT_TOKENS",
        gt=0,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
