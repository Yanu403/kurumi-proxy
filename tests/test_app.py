import json

import pytest
from httpx import ASGITransport, AsyncClient

from kurumi_proxy.config import Settings
from kurumi_proxy.main import app, get_app_settings
from kurumi_proxy.providers.base import ProviderResult


class FakeProvider:
    def __init__(self, settings: Settings):
        self.settings = settings

    async def complete(self, messages: object, model: str | None = None) -> ProviderResult:
        return ProviderResult(text="mocked response", model=model or "default-model")


@pytest.fixture(autouse=True)
def reset_app_state() -> None:
    app.dependency_overrides.clear()
    app.state.provider_factory = FakeProvider
    yield
    app.dependency_overrides.clear()
    if hasattr(app.state, "provider_factory"):
        del app.state.provider_factory


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
        return Settings(CODEBUDDY_MODEL="custom-model")

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
        return Settings(KURUMI_PROXY_API_KEY="downstream-secret")

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
async def test_missing_upstream_credential_returns_503() -> None:
    if hasattr(app.state, "provider_factory"):
        del app.state.provider_factory

    async def override_settings() -> Settings:
        return Settings(CODEBUDDY_API_KEY=None)

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
