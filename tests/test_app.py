import json

import pytest
from httpx import ASGITransport, AsyncClient

from kurumi_proxy.config import Settings
from kurumi_proxy.db import UsageStore
from kurumi_proxy.main import app, get_app_settings
from kurumi_proxy.providers.base import ProviderBadGatewayError, ProviderResult


class FakeProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def complete(self, messages: object, model: str | None = None) -> ProviderResult:
        return ProviderResult(text="mocked response", model=model or "gemini-2.5-flash-lite")


@pytest.fixture(autouse=True)
def reset_app_state(tmp_path) -> None:
    async def override_settings() -> Settings:
        return Settings(_env_file=None, KURUMI_PROXY_API_KEY=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "test.sqlite3"))

    app.dependency_overrides.clear()
    app.dependency_overrides[get_app_settings] = override_settings
    app.state.provider_factory = FakeProvider
    yield
    app.dependency_overrides.clear()
    if hasattr(app.state, "provider_factory"):
        del app.state.provider_factory
    if hasattr(app.state, "usage_store"):
        del app.state.usage_store


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
async def test_models_returns_merlin_models() -> None:
    async with client() as ac:
        response = await ac.get("/v1/models")

    assert response.status_code == 200
    data = response.json()["data"]
    ids = {model["id"] for model in data}
    assert {"gemini-2.5-flash-lite", "gemini-2.5-pro", "gpt-5.5"} <= ids
    # owned_by should be "merlin"
    assert all(model["owned_by"] == "merlin" for model in data)


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
async def test_chat_completion_default_model() -> None:
    """When model is omitted, uses merlin_default_model."""
    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

    body = response.json()
    assert response.status_code == 200
    assert body["model"] == "gemini-2.5-flash-lite"


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
        return Settings(_env_file=None, KURUMI_PROXY_API_KEY="downstream-secret")

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
async def test_provider_error_surfaces_as_502(tmp_path) -> None:
    class ErrorProvider:
        def __init__(self, settings: Settings):
            self.settings = settings

        async def complete(self, messages: object, model: str | None = None) -> ProviderResult:
            raise ProviderBadGatewayError("upstream exploded")

    app.state.provider_factory = ErrorProvider

    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={"messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 502
    body = response.json()
    assert body["error"]["type"] == "provider_error"
    assert "upstream exploded" in body["error"]["message"]


@pytest.mark.asyncio
async def test_provider_error_is_recorded_in_usage(tmp_path) -> None:
    class ErrorProvider:
        def __init__(self, settings: Settings):
            self.settings = settings

        async def complete(self, messages: object, model: str | None = None) -> ProviderResult:
            raise ProviderBadGatewayError("quota exhausted")

    app.state.provider_factory = ErrorProvider

    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 502
    # Usage should be recorded as an error
    store = UsageStore(Settings(_env_file=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "test.sqlite3")))
    store.init()
    usage = store.usage_summary(days=1)["items"]
    assert len(usage) == 1
    assert usage[0]["errors"] == 1


@pytest.mark.asyncio
async def test_chat_completion_records_usage(tmp_path) -> None:
    async with client() as ac:
        response = await ac.post(
            "/v1/chat/completions",
            json={"model": "gpt-5.5", "messages": [{"role": "user", "content": "hello"}]},
        )

    assert response.status_code == 200
    store = UsageStore(Settings(_env_file=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "test.sqlite3")))
    store.init()
    usage = store.usage_summary(days=1)["items"]
    assert len(usage) == 1
    assert usage[0]["requests"] == 1
    assert usage[0]["total_tokens"] >= 2


@pytest.mark.asyncio
async def test_admin_usage_and_quota(tmp_path) -> None:
    store = UsageStore(Settings(_env_file=None, KURUMI_PROXY_DB_PATH=str(tmp_path / "usage.sqlite3")))
    store.record_usage(
        model="gpt-5.5",
        endpoint="/v1/chat/completions",
        prompt_tokens=4,
        completion_tokens=2,
        total_tokens=6,
        status="success",
    )
    app.state.usage_store = store

    async with client() as ac:
        usage = await ac.get("/admin/usage?days=7")
        quota = await ac.get("/admin/quota")

    assert usage.status_code == 200
    assert usage.json()["items"][0]["total_tokens"] == 6
    assert quota.status_code == 200
    assert quota.json()["totals"]["all_time"]["total_tokens"] == 6
