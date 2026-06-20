import asyncio
import os
import re
from collections.abc import Sequence

from kurumi_proxy.config import Settings
from kurumi_proxy.models import ChatMessage, GenericContentBlock, TextContentBlock
from kurumi_proxy.providers.base import (
    BaseProvider,
    MissingCredentialError,
    ProviderBadGatewayError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
)

KNOWN_MODELS = [
    "default-model",
    "gemini-3.1-pro",
    "gemini-3.0-flash",
    "gemini-3.5-flash",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-3.1-flash-lite",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.3-codex",
    "gpt-5.1-codex",
    "gpt-5.1-codex-mini",
    "deepseek-v3-2-volc",
    "glm-5.0",
    "kimi-k2.5",
    "claude-opus-4.6",
    "claude-sonnet-4.6",
]

_SECRET_PATTERNS = [
    re.compile(r"(CODEBUDDY_API_KEY=)[^\s]+", re.IGNORECASE),
    re.compile(r"(api[_-]?key[=:]\s*)[^\s]+", re.IGNORECASE),
    re.compile(r"(bearer\s+)[a-z0-9._~+/=-]+", re.IGNORECASE),
]


def redact_secrets(value: str) -> str:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        redacted = pattern.sub(r"\1[REDACTED]", redacted)
    return redacted


def message_content_to_text(message: ChatMessage) -> str:
    content = message.content
    if content is None:
        return ""
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content:
        if isinstance(block, TextContentBlock):
            parts.append(block.text)
        elif isinstance(block, GenericContentBlock):
            parts.append(f"[Unsupported content block ignored: {block.type}]")
        else:
            parts.append("[Unsupported content block ignored]")
    return "\n".join(parts)


def build_prompt(messages: Sequence[ChatMessage]) -> str:
    system_parts: list[str] = []
    conversation_lines: list[str] = []
    latest_user: str | None = None

    for message in messages:
        content = message_content_to_text(message)
        role = message.role.lower()
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            latest_user = content
            conversation_lines.append(f"User: {content}")
        elif role == "assistant":
            conversation_lines.append(f"Assistant: {content}")
        else:
            conversation_lines.append(f"{message.role.title()}: {content}")

    prompt_parts: list[str] = []
    if system_parts:
        prompt_parts.append("System:\n" + "\n\n".join(system_parts).strip())
    if conversation_lines:
        prompt_parts.append("Conversation:\n" + "\n".join(conversation_lines).strip())
    if latest_user is not None:
        prompt_parts.append("User:\n" + latest_user.strip())

    return "\n\n".join(part for part in prompt_parts if part.strip()).strip()


class CodeBuddyProvider(BaseProvider):
    provider_id = "codebuddy"
    provider_name = "CodeBuddy"

    def __init__(self, settings: Settings):
        self.settings = settings

    def list_models(self) -> list[str]:
        return list(KNOWN_MODELS)

    def command(self, prompt: str, model: str) -> list[str]:
        return [
            self.settings.codebuddy_bin,
            "-p",
            "--tools",
            "",
            "--model",
            model,
            "--output-format",
            "text",
            "--input-format",
            "text",
        ]

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        model: str | None = None,
        *,
        api_key: str | None = None,
    ) -> ProviderResult:
        upstream_api_key = api_key or self.settings.codebuddy_api_key
        if not upstream_api_key:
            raise MissingCredentialError("CODEBUDDY_API_KEY is required for upstream CodeBuddy calls.")

        selected_model = model or self.settings.codebuddy_model
        prompt = build_prompt(messages)
        env = os.environ.copy()
        env["CODEBUDDY_API_KEY"] = upstream_api_key

        try:
            process = await asyncio.create_subprocess_exec(
                *self.command(prompt, selected_model),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise ProviderUnavailableError(
                f"CodeBuddy command not found: {self.settings.codebuddy_bin}"
            ) from exc
        except OSError as exc:
            raise ProviderBadGatewayError("CodeBuddy command could not be started.") from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(input=prompt.encode("utf-8")),
                timeout=self.settings.codebuddy_timeout_seconds,
            )
        except TimeoutError as exc:
            process.kill()
            try:
                await asyncio.wait_for(process.communicate(), timeout=1)
            except TimeoutError:
                pass
            raise ProviderTimeoutError("CodeBuddy command timed out.") from exc

        stdout_text = stdout.decode("utf-8", errors="replace").strip()
        stderr_text = stderr.decode("utf-8", errors="replace").strip()

        if process.returncode != 0:
            summary = redact_secrets(stderr_text or stdout_text or f"exit code {process.returncode}")
            if len(summary) > 500:
                summary = summary[:497] + "..."
            raise ProviderBadGatewayError(f"CodeBuddy command failed: {summary}")

        return ProviderResult(text=stdout_text, model=selected_model)
