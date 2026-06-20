from kurumi_proxy.config import Settings


def test_settings_defaults_do_not_require_credentials() -> None:
    settings = Settings(_env_file=None, KURUMI_PROXY_API_KEY=None)

    assert settings.kurumi_proxy_max_output_tokens == 8192
    assert settings.kurumi_proxy_db_path == "runtime/kurumi_proxy.sqlite3"
    assert settings.kurumi_proxy_rtk_enabled is True
    assert settings.merlin_default_model == "gemini-2.5-flash-lite"
    assert settings.merlin_base_url == "https://www.getmerlin.in"
    assert settings.merlin_firebase_api_key == "AIzaSyAvCgtQ4XbmlQGIynDT-v_M8eLaXrKmtiM"
    assert settings.merlin_refresh_token is None
    assert settings.merlin_email is None
    assert settings.merlin_password is None
