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

    # --- Multi-provider dispatch -------------------------------------------------
    # When a request omits the ``provider/model`` prefix, route it here.
    kurumi_proxy_default_provider: str = Field(
        default="codebuddy",
        alias="KURUMI_PROXY_DEFAULT_PROVIDER",
        pattern="^(codebuddy|merlin)$",
    )

    # --- Merlin (getmerlin.in) provider ------------------------------------------
    merlin_firebase_api_key: str = Field(
        default="AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM",
        alias="MERLIN_FIREBASE_API_KEY",
    )
    merlin_firebase_project: str = Field(
        default="foyer-work",
        alias="MERLIN_FIREBASE_PROJECT",
    )
    # Optional: a long-lived Firebase idToken/refreshToken captured from a Pro
    # session. When set, the provider skips anonymous sign-up and refreshes
    # this token instead, unlocking Pro models. Format: "idToken|refreshToken".
    merlin_refresh_token: str | None = Field(default=None, alias="MERLIN_REFRESH_TOKEN")
    merlin_default_model: str = Field(
        default="gemini-2.5-flash-lite",
        alias="MERLIN_DEFAULT_MODEL",
    )
    merlin_base_url: str = Field(
        default="https://www.getmerlin.in",
        alias="MERLIN_BASE_URL",
    )
    merlin_cdn_models_url: str = Field(
        default="https://cdn.jsdelivr.net/gh/foyer-work/cdn-files@latest/merlin_constants.json",
        alias="MERLIN_CDN_MODELS_URL",
    )
    merlin_request_timeout_seconds: float = Field(
        default=180,
        alias="MERLIN_REQUEST_TIMEOUT_SECONDS",
        gt=0,
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
