import json

import pytest
from httpx import ASGITransport, AsyncClient

from kurumi_proxy.config import Settings
from kurumi_proxy.db import ConnectionStore
from kurumi_proxy.main import app, get_app_settings
from kurumi_proxy.providers.base import ProviderBadGatewayError, ProviderResult


class FakeProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def complete(self, messages: object, model: str | None = None) -> ProviderResult:
        return ProviderResult(text="mocked response", model=model or "default-model")


@pytest.fixture(autouse=True)
def reset_app_state(tmp_path) -> None:
    async def override_settings() -> Settings:
        return Settings(_env_file=None, KURUMI_PROXY_API_KEY=None, CODEBUDDY_API_KEY=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "test.sqlite3"))

    app.dependency_overrides.clear()
    app.dependency_overrides[get_app_settings] = override_settings
    app.state.provider_factory = FakeProvider
    yield
    app.dependency_overrides.clear()
    if hasattr(app.state, "provider_factory"):
        del app.state.provider_factory
    if hasattr(app.state, "connection_store"):
        del app.state.connection_store


def client() -> AsyncClient:
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_health_ok() -> None:
    async with client() as ac:
        response = await ac.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_models_include_codebuddy_ids() -> None:
    async def override_settings() -> Settings:
        return Settings(_env_file=None, KURUMI_PROXY_API_KEY=None, CODEBUDDY_API_KEY=None, CODEBUDDY_MODEL="custom-model")

    app.dependency_overrides[get_app_settings] = override_settings

    async with client() as ac:
        response = await ac.get("/v1/models")

    assert response.status_code == 200
    data = response.json()["data"]
    ids = {model["id"] for model in data}
    assert {"custom-model", "default-model", "gpt-5.5", "gemini-3.1-pro"} <= ids


@pytest.mark.asyncio
async def test_chat_completion_openai_shape() -> None:
    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["id"].startswith("chatcmpl-")
    assert body["object"] == "chat.completion"
    assert body["model"] == "gpt-5.5"
    assert body["choices"][0]["message"] == {"role": "assistant", "content": "mocked response"}
    assert body["usage"]["total_tokens"] >= body["usage"]["completion_tokens"] >= 1


@pytest.mark.asyncio
async def test_chat_completion_rejects_tool_calls_before_provider() -> None:
    class CountingProvider:
        calls = 0

        def __init__(self, settings: Settings):
            self.settings = settings

        async def complete(self, messages: object, model: str | None = None) -> ProviderResult:
            CountingProvider.calls += 1
            return ProviderResult(text="should not be called", model=model or "default-model")

    app.state.provider_factory = CountingProvider

    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "Use a tool."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_status",
                            "description": "Get repository status.",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                "tool_choice": "auto",
            },
        )

    assert response.status_code == 400
    body = response.json()
    assert body == {
        "error": {
            "message": "Kurumi Proxy is text-only and does not support tool_calls yet. Remove tools/tool_choice and send a text-only chat completion request.",
            "type": "invalid_request_error",
            "param": "tools",
            "code": "unsupported_tool_calls",
        }
    }
    assert CountingProvider.calls == 0


@pytest.mark.asyncio
async def test_chat_completion_allows_empty_tools_array() -> None:
    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={
                "model": "gpt-5.5",
                "messages": [{"role": "user", "content": "hello"}],
                "tools": [],
                "tool_choice": "none",
            },
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "mocked response"


@pytest.mark.asyncio
async def test_streaming_chat_completion_openai_sse_shape() -> None:
    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={"stream": True, "model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"

    chunks = [json.loads(event) for event in events[:-1]]
    assert [chunk["object"] for chunk in chunks] == ["chat.completion.chunk"] * 3
    assert {chunk["id"] for chunk in chunks} == {chunks[0]["id"]}
    assert {chunk["model"] for chunk in chunks} == {"gpt-5.5"}
    assert chunks[0]["choices"] == [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]
    assert chunks[1]["choices"] == [{"index": 0, "delta": {"content": "mocked response"}, "finish_reason": None}]
    assert chunks[2]["choices"] == [{"index": 0, "delta": {}, "finish_reason": "stop"}]


@pytest.mark.asyncio
async def test_downstream_api_key_enforcement() -> None:
    async def override_settings() -> Settings:
        return Settings(_env_file=None, KURUMI_PROXY_API_KEY="downstream-secret", CODEBUDDY_API_KEY=None)

    app.dependency_overrides[get_app_settings] = override_settings

    async with client() as ac:
        missing = await ac.get("/v1/models")
        wrong = await ac.get("/v1/models", headers={"Authorization": "Bearer wrong"})
        ok = await ac.get("/v1/models", headers={"Authorization": "Bearer downstream-secret"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.json()["error"]["type"] == "authentication_error"
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_missing_upstream_credential_returns_503(tmp_path) -> None:
    if hasattr(app.state, "provider_factory"):
        del app.state.provider_factory

    async def override_settings() -> Settings:
        return Settings(_env_file=None, KURUMI_PROXY_API_KEY=None, CODEBUDDY_API_KEY=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "missing.sqlite3"))

    app.dependency_overrides[get_app_settings] = override_settings

    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 503
    body = response.json()
    assert body["error"]["type"] == "provider_error"
    assert "CODEBUDDY_API_KEY" in body["error"]["message"]


def test_db_initializes_and_seeds_env_key(tmp_path) -> None:
    store = ConnectionStore(
        Settings(_env_file=None, KURUMI_PROXY_API_KEY=None, CODEBUDDY_API_KEY="seed-secret", KURUMI_PROXY_DB_PATH=str(tmp_path / "seed.sqlite3"))
    )
    connections = store.list_connections()

    assert len(connections) == 1
    assert connections[0].id == "env-default"
    assert connections[0].name == "env-default"
    assert connections[0].api_key == "seed-secret"
    assert "api_key" not in connections[0].safe_dict()


@pytest.mark.asyncio
async def test_admin_connections_never_return_raw_api_key(tmp_path) -> None:
    settings = Settings(_env_file=None, KURUMI_PROXY_API_KEY=None, CODEBUDDY_API_KEY=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "admin.sqlite3"))
    store = ConnectionStore(settings)
    app.state.connection_store = store

    async with client() as ac:
        created = await ac.post(
            "/admin/connections",
            json={"name": "primary", "api_key": "upstream-secret", "priority": 10},
        )
        listed = await ac.get("/admin/connections")

    assert created.status_code == 200
    assert listed.status_code == 200
    assert created.json()["name"] == "primary"
    assert "upstream-secret" not in created.text
    assert "api_key" not in created.json()
    assert "upstream-secret" not in listed.text
    assert listed.json()["data"][0]["priority"] == 10


@pytest.mark.asyncio
async def test_fallback_retries_second_key_and_records_usage(tmp_path) -> None:
    settings = Settings(_env_file=None, KURUMI_PROXY_API_KEY=None, CODEBUDDY_API_KEY=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "fallback.sqlite3"))
    store = ConnectionStore(settings)
    store.create_connection(name="bad", api_key="bad-key", priority=1)
    store.create_connection(name="good", api_key="good-key", priority=2)
    app.state.connection_store = store

    class FallbackProvider:
        calls: list[str | None] = []

        def __init__(self, settings: Settings):
            self.settings = settings

        async def complete(self, messages: object, model: str | None = None, *, api_key: str | None = None) -> ProviderResult:
            self.calls.append(api_key)
            if api_key == "bad-key":
                raise ProviderBadGatewayError("quota exhausted")
            return ProviderResult(text="fallback ok", model=model or "default-model")

    app.state.provider_factory = FallbackProvider

    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "fallback ok"
    assert FallbackProvider.calls == ["bad-key", "good-key"]
    usage = store.usage_summary(days=1)["items"]
    assert sum(item["requests"] for item in usage) == 2
    assert sum(item["errors"] for item in usage) == 1


@pytest.mark.asyncio
async def test_all_connections_unavailable_returns_provider_error(tmp_path) -> None:
    settings = Settings(_env_file=None, KURUMI_PROXY_API_KEY=None, CODEBUDDY_API_KEY=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "locked.sqlite3"))
    store = ConnectionStore(settings)
    connection = store.create_connection(name="locked", api_key="locked-key", priority=1)
    store.mark_failure(connection, model="gpt-5.5", error="quota exhausted", category="quota")
    app.state.connection_store = store

    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 503
    assert response.json()["error"]["type"] == "provider_error"


@pytest.mark.asyncio
async def test_admin_usage_and_quota(tmp_path) -> None:
    store = ConnectionStore(Settings(_env_file=None, KURUMI_PROXY_API_KEY=None, CODEBUDDY_API_KEY=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "usage.sqlite3")))
    connection = store.create_connection(name="primary", api_key="secret", priority=1)
    store.record_usage(
        model="gpt-5.5",
        connection=connection,
        endpoint="/v1/chat/completions",
        prompt_tokens=4,
        completion_tokens=2,
        total_tokens=6,
        status="success",
    )
    app.state.connection_store = store

    async with client() as ac:
        usage = await ac.get("/admin/usage?days=7")
        quota = await ac.get("/admin/quota")

    assert usage.status_code == 200
    assert usage.json()["items"][0]["total_tokens"] == 6
    assert quota.status_code == 200
    assert quota.json()["credit_balance_known"] is False
    assert quota.json()["totals"]["all_time"]["total_tokens"] == 6
