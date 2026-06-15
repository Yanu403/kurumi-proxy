from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    kurumi_proxy_api_key: str | None = Field(default=None, alias="KURUMI_PROXY_API_KEY")
    codebuddy_api_key: str | None = Field(default=None, alias="CODEBUDDY_API_KEY")
    codebuddy_bin: str = Field(default="codebuddy", alias="CODEBUDDY_BIN")
    codebuddy_model: str = Field(default="default-model", alias="CODEBUDDY_MODEL")
    kurumi_proxy_backend: str = Field(default="subprocess", alias="KURUMI_PROXY_BACKEND", pattern="^(acp|subprocess)$")
    codebuddy_daemon_port: int = Field(default=6275, alias="CODEBUDDY_DAEMON_PORT", ge=1024, le=65535)
    codebuddy_timeout_seconds: float = Field(default=180, alias="CODEBUDDY_TIMEOUT_SECONDS", gt=0)
    kurumi_proxy_max_output_tokens: int = Field(
        default=8192,
        alias="KURUMI_PROXY_MAX_OUTPUT_TOKENS",
        gt=0,
    )
    kurumi_proxy_db_path: str = Field(
        default="runtime/kurumi_proxy.sqlite3",
        alias="KURUMI_PROXY_DB_PATH",
    )
    kurumi_proxy_routing_strategy: str = Field(
        default="fill-first",
        alias="KURUMI_PROXY_ROUTING_STRATEGY",
        pattern="^(fill-first|round-robin)$",
    )
    kurumi_proxy_sticky_round_robin_limit: int = Field(
        default=3,
        alias="KURUMI_PROXY_STICKY_ROUND_ROBIN_LIMIT",
        ge=1,
    )
    kurumi_proxy_rtk_enabled: bool = Field(default=True, alias="KURUMI_PROXY_RTK_ENABLED")
    kurumi_proxy_rtk_min_bytes: int = Field(default=2000, alias="KURUMI_PROXY_RTK_MIN_BYTES", ge=1)
    kurumi_proxy_rtk_max_bytes: int = Field(default=200000, alias="KURUMI_PROXY_RTK_MAX_BYTES", ge=1)
    kurumi_proxy_rtk_head_lines: int = Field(default=120, alias="KURUMI_PROXY_RTK_HEAD_LINES", ge=1)
    kurumi_proxy_rtk_tail_lines: int = Field(default=80, alias="KURUMI_PROXY_RTK_TAIL_LINES", ge=1)


@lru_cache
def get_settings() -> Settings:
    return Settings()
