import asyncio

import pytest

from kurumi_proxy.config import Settings
from kurumi_proxy.models import ChatMessage
from kurumi_proxy.providers.base import (
    MissingCredentialError,
    ProviderBadGatewayError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from kurumi_proxy.providers.codebuddy import CodeBuddyProvider, redact_secrets


def test_provider_command_shape() -> None:
    provider = CodeBuddyProvider(Settings(CODEBUDDY_BIN="codebuddy-test", CODEBUDDY_API_KEY="secret"))

    assert provider.command("hello", "gpt-5.5") == [
        "codebuddy-test",
        "-p",
        "--tools",
        "",
        "--model",
        "gpt-5.5",
        "--output-format",
        "text",
        "--input-format",
        "text",
    ]


@pytest.mark.asyncio
async def test_missing_codebuddy_key_raises_before_subprocess() -> None:
    provider = CodeBuddyProvider(Settings(CODEBUDDY_API_KEY=None))

    with pytest.raises(MissingCredentialError):
        await provider.complete([ChatMessage(role="user", content="hello")])


@pytest.mark.asyncio
async def test_provider_success(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    class FakeProcess:
        returncode = 0

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            calls["input"] = input
            return b"ok", b""

    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> FakeProcess:
        calls["args"] = args
        calls["env"] = kwargs["env"]
        calls["stdin"] = kwargs["stdin"]
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    provider = CodeBuddyProvider(Settings(CODEBUDDY_API_KEY="secret"))

    result = await provider.complete([ChatMessage(role="user", content="hello")], "gpt-5.5", api_key="call-secret")

    assert result.text == "ok"
    assert result.model == "gpt-5.5"
    args = calls["args"]
    assert isinstance(args, tuple)
    assert args[0] == "codebuddy"
    assert "hello" not in args
    assert calls["stdin"] == asyncio.subprocess.PIPE
    assert calls["input"] == b"Conversation:\nUser: hello\n\nUser:\nhello"
    assert calls["env"]["CODEBUDDY_API_KEY"] == "call-secret"  # type: ignore[index]


@pytest.mark.asyncio
async def test_provider_command_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> object:
        raise FileNotFoundError

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    provider = CodeBuddyProvider(Settings(CODEBUDDY_API_KEY="secret"))

    with pytest.raises(ProviderUnavailableError):
        await provider.complete([ChatMessage(role="user", content="hello")])


@pytest.mark.asyncio
async def test_provider_subprocess_oserror_is_bad_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> object:
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    provider = CodeBuddyProvider(Settings(CODEBUDDY_API_KEY="secret"))

    with pytest.raises(ProviderBadGatewayError) as exc_info:
        await provider.complete([ChatMessage(role="user", content="hello")])

    assert "Argument list too long" not in exc_info.value.message


@pytest.mark.asyncio
async def test_provider_nonzero_exit_redacts_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        returncode = 1

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            return b"", b"api_key=secret-value failed"

    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> FakeProcess:
        return FakeProcess()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    provider = CodeBuddyProvider(Settings(CODEBUDDY_API_KEY="secret"))

    with pytest.raises(ProviderBadGatewayError) as exc_info:
        await provider.complete([ChatMessage(role="user", content="hello")])

    assert "secret-value" not in exc_info.value.message
    assert "[REDACTED]" in exc_info.value.message


@pytest.mark.asyncio
async def test_provider_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeProcess:
        returncode = None
        killed = False

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            await asyncio.sleep(10)
            return b"", b""

        def kill(self) -> None:
            self.killed = True

    process = FakeProcess()

    async def fake_create_subprocess_exec(*args: str, **kwargs: object) -> FakeProcess:
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    provider = CodeBuddyProvider(Settings(CODEBUDDY_API_KEY="secret", CODEBUDDY_TIMEOUT_SECONDS=0.01))

    with pytest.raises(ProviderTimeoutError):
        await provider.complete([ChatMessage(role="user", content="hello")])

    assert process.killed is True


def test_redact_secrets() -> None:
    assert redact_secrets("Authorization: Bearer abc123") == "Authorization: Bearer [REDACTED]"
