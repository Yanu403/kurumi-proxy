from kurumi_proxy.config import Settings


def test_settings_defaults_do_not_require_credentials() -> None:
    settings = Settings()

    assert settings.codebuddy_bin == "codebuddy"
    assert settings.codebuddy_model == "default-model"
    assert settings.codebuddy_timeout_seconds == 180
    assert settings.kurumi_proxy_max_output_tokens == 8192
    assert settings.codebuddy_api_key is None
