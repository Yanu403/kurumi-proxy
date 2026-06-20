"""Merlin AI (getmerlin.in) provider.

Merlin is a browser-extension/web-app that proxies LLM requests through its
backend at ``www.getmerlin.in``. Authentication is via **Firebase Auth**
(project ``foyer-work``); anonymous sign-up yields a short-lived ``idToken``
that we attach as ``Authorization: Bearer <idToken>`` to the chat endpoint.

Wire-format ground truth: ``docs/MERLIN_PROTOCOL.md`` and
``docs/wire-samples/merlin-chat-capture.json``.

The SSE response format is not yet captured in full, so the SSE parser is
deliberately permissive: it reads ``data:`` lines, attempts JSON parsing,
and extracts content from a handful of common shapes before falling back to
raw text. The first few events are logged at DEBUG to aid future debugging.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone

import httpx

from kurumi_proxy.config import Settings
from kurumi_proxy.models import ChatMessage
from kurumi_proxy.providers.base import (
    BaseProvider,
    ProviderAuthError,
    ProviderBadGatewayError,
    ProviderRateLimitError,
    ProviderResult,
    ProviderTimeoutError,
    ProviderUnavailableError,
    StreamDelta,
)
from kurumi_proxy.providers.codebuddy import message_content_to_text

logger = logging.getLogger(__name__)

# Firebase endpoints (key is project-specific but public, shipped in the
# Merlin web bundle).
_FIREBASE_SIGNUP_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={key}"
)
_FIREBASE_SIGNIN_URL = (
    "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={key}"
)
_FIREBASE_TOKEN_URL = (
    "https://securetoken.googleapis.com/v1/token?key={key}"
)

# Browser-like headers so the upstream WAF/Cloudflare treats us as the web app.
_DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36"
)


def _now_iso() -> str:
    """ISO-8601 timestamp with offset, matching Merlin's ``x-request-timestamp``.

    The capture uses ``+07:00[Asia/Jakarta]``; we emit local-offset form which
    the backend accepts.
    """
    return datetime.now(timezone.utc).astimezone().isoformat()


class MerlinAuth:
    """Manages a Firebase ``idToken`` with lazy sign-up and auto-refresh.

    A token is fetched on first use and refreshed when it is within a safety
    margin of expiry. All network access is async and goes through the
    ``httpx.AsyncClient`` owned by the provider, so tests can mock the client.
    """

    # Refresh a bit before the real expiry to avoid races.
    _refresh_margin_seconds = 60

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self._client = client
        self._id_token: str | None = None
        self._refresh_token: str | None = settings.merlin_refresh_token
        # Expiry epoch seconds (monotonic-free, wall clock). 0 = unknown/expired.
        self._expires_at: float = 0.0
        # Serialize token fetches so concurrent requests share one sign-up.
        self._lock = asyncio.Lock()

    @property
    def id_token(self) -> str | None:
        return self._id_token

    def _is_expired(self) -> bool:
        if self._id_token is None:
            return True
        return time.time() >= self._expires_at - self._refresh_margin_seconds

    async def get_token(self) -> str:
        """Return a valid idToken, signing in or refreshing as needed."""
        if not self._is_expired():
            assert self._id_token is not None
            return self._id_token
        async with self._lock:
            if not self._is_expired():
                assert self._id_token is not None
                return self._id_token
            if self._refresh_token is not None and self._id_token is not None:
                await self._refresh()
            elif self.settings.merlin_email and self.settings.merlin_password:
                await self._sign_in_with_password()
            else:
                await self._sign_up_anonymous()
            assert self._id_token is not None
            return self._id_token

    async def _sign_in_with_password(self) -> None:
        """Sign in with email/password to get a Pro token."""
        url = _FIREBASE_SIGNIN_URL.format(key=self.settings.merlin_firebase_api_key)
        try:
            resp = await self._client.post(
                url,
                json={
                    "email": self.settings.merlin_email,
                    "password": self.settings.merlin_password,
                    "returnSecureToken": True,
                },
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"Firebase sign-in request failed: {exc}") from exc
        if resp.status_code >= 500:
            raise ProviderUnavailableError(f"Firebase sign-in unavailable (HTTP {resp.status_code}).")
        if resp.status_code >= 400:
            raise ProviderAuthError(f"Firebase sign-in rejected (HTTP {resp.status_code}). Check MERLIN_EMAIL/MERLIN_PASSWORD.")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderBadGatewayError("Firebase sign-in returned non-JSON.") from exc
        self._apply_tokens(
            id_token=payload.get("idToken"),
            refresh_token=payload.get("refreshToken"),
            expires_in=payload.get("expiresIn", "3600"),
        )

    async def _sign_up_anonymous(self) -> None:
        url = _FIREBASE_SIGNUP_URL.format(key=self.settings.merlin_firebase_api_key)
        try:
            resp = await self._client.post(
                url,
                json={"returnSecureToken": True},
                headers={"Content-Type": "application/json"},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"Firebase sign-up request failed: {exc}") from exc
        if resp.status_code >= 500:
            raise ProviderUnavailableError(f"Firebase sign-up unavailable (HTTP {resp.status_code}).")
        if resp.status_code >= 400:
            raise ProviderAuthError(f"Firebase sign-up rejected (HTTP {resp.status_code}).")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderBadGatewayError("Firebase sign-up returned non-JSON.") from exc
        self._apply_tokens(
            id_token=payload.get("idToken"),
            refresh_token=payload.get("refreshToken"),
            expires_in=payload.get("expiresIn", "3600"),
        )

    async def _refresh(self) -> None:
        url = _FIREBASE_TOKEN_URL.format(key=self.settings.merlin_firebase_api_key)
        try:
            resp = await self._client.post(
                url,
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": self._refresh_token,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"Firebase refresh request failed: {exc}") from exc
        if resp.status_code >= 500:
            raise ProviderUnavailableError(f"Firebase refresh unavailable (HTTP {resp.status_code}).")
        if resp.status_code in (400, 401, 403):
            # Refresh token is dead — fall back to anonymous sign-up so the
            # provider self-heals instead of hard-failing.
            logger.warning("Merlin refresh token rejected; re-authenticating anonymously.")
            self._id_token = None
            self._refresh_token = None
            await self._sign_up_anonymous()
            return
        if resp.status_code >= 400:
            raise ProviderAuthError(f"Firebase refresh rejected (HTTP {resp.status_code}).")
        try:
            payload = resp.json()
        except ValueError as exc:
            raise ProviderBadGatewayError("Firebase refresh returned non-JSON.") from exc
        self._apply_tokens(
            id_token=payload.get("id_token") or payload.get("access_token"),
            refresh_token=payload.get("refresh_token"),
            expires_in=payload.get("expires_in", "3600"),
        )

    def _apply_tokens(self, *, id_token: str | None, refresh_token: str | None, expires_in) -> None:
        if not id_token:
            raise ProviderBadGatewayError("Firebase response missing idToken.")
        self._id_token = id_token
        if refresh_token:
            self._refresh_token = refresh_token
        try:
            ttl = int(float(expires_in))
        except (TypeError, ValueError):
            ttl = 3600
        self._expires_at = time.time() + ttl

    def invalidate(self) -> None:
        """Force the next ``get_token`` to re-authenticate."""
        self._id_token = None
        self._expires_at = 0.0


# ---------------------------------------------------------------------------
# Request translation
# ---------------------------------------------------------------------------

def build_merlin_request(
    messages: Sequence[ChatMessage],
    model: str,
) -> dict:
    """Translate OpenAI-style messages into the Merlin unified-chat body.

    Merlin's API takes a single ``message.content`` string; we collapse the
    full conversation into that string using the same role-tagged layout the
    CodeBuddy provider uses, so multi-turn context is preserved.
    """
    parts: list[str] = []
    for message in messages:
        role = message.role.lower()
        text = message_content_to_text(message)
        if not text:
            continue
        parts.append(f"{role}: {text}")
    content = "\n\n".join(parts).strip()

    return {
        "attachments": [],
        "chatId": str(uuid.uuid4()),
        "language": "AUTO",
        "message": {
            "childId": str(uuid.uuid4()),
            "content": content,
            "context": "",
            "id": str(uuid.uuid4()),
            "parentId": "root",
        },
        "mode": "UNIFIED_CHAT",
        "model": model,
        "metadata": {
            "noTask": True,
            "isWebpageChat": False,
            "deepResearch": False,
            "webAccess": True,
            "proFinderMode": False,
            "mcpConfig": {"isEnabled": False},
            "merlinMagic": False,
        },
    }


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------

def parse_sse_event(data: str) -> dict | str | None:
    """Parse one SSE ``data:`` payload.

    Returns the decoded JSON object if it parses, the raw string if it is a
    non-JSON sentinel (e.g. ``[DONE]``), or ``None`` for empty data.
    """
    if data == "":
        return None
    if data == "[DONE]":
        return "[DONE]"
    try:
        return json.loads(data)
    except ValueError:
        return data


def extract_content(event: dict | str) -> str:
    """Best-effort extraction of incremental text from one SSE event.

    Handles both OpenAI-style shapes and Merlin's own nesting where content
    lives at ``data.data.text``. Unknown shapes contribute empty string rather
    than raising, so a single surprising event never kills the whole stream.
    """
    if isinstance(event, str):
        return "" if event == "[DONE]" else event
    if not isinstance(event, dict):
        return ""

    # Merlin format: {"data":{"content":"","index":1,"type":"text","text":"Hello"}}
    inner = event.get("data")
    if isinstance(inner, dict):
        # Skip DONE signals
        if inner.get("eventType") == "DONE":
            return ""
        text = inner.get("text")
        if isinstance(text, str) and text:
            return text
        content = inner.get("content")
        if isinstance(content, str) and content:
            return content

    # OpenAI-style: choices[0].delta.content
    choices = event.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            delta = first.get("delta")
            if isinstance(delta, dict) and delta.get("content"):
                return delta["content"]
            msg = first.get("message")
            if isinstance(msg, dict) and msg.get("content"):
                return msg["content"]
            text = first.get("text")
            if isinstance(text, str):
                return text

    # Common flat shapes.
    for key in ("content", "text", "delta", "message"):
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            inner2 = value.get("content") or value.get("text")
            if isinstance(inner2, str):
                return inner2
    return ""


async def iter_sse_events(line_iter: AsyncIterator[str]) -> AsyncIterator[tuple[str, str]]:
    """Yield ``(event_type, data_payload)`` tuples from an SSE stream.

    ``line_iter`` yields text lines (already decoded). Events are separated by
    blank lines. Each event may have an ``event:`` field (defaults to
    ``"message"`` per the SSE spec) and one or more ``data:`` fields which are
    concatenated.
    """
    event_type = "message"
    data_lines: list[str] = []
    async for line in line_iter:
        line = line.rstrip("\r\n")
        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            payload = line[len("data:"):]
            if payload.startswith(" "):
                payload = payload[1:]
            data_lines.append(payload)
        elif line == "":
            if data_lines:
                yield event_type, "\n".join(data_lines)
                data_lines = []
                event_type = "message"
    if data_lines:
        yield event_type, "\n".join(data_lines)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class MerlinProvider(BaseProvider):
    provider_id = "merlin"
    provider_name = "Merlin AI"

    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None):
        self.settings = settings
        # The provider owns the client so it can close it on shutdown. Tests
        # inject a mock/transport-equipped client instead.
        self._client = client
        self._owns_client = client is None
        self._auth = MerlinAuth(settings, self._ensure_client())
        self._models_cache: list[str] | None = None

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self.settings.merlin_request_timeout_seconds,
                headers={
                    "User-Agent": _DEFAULT_UA,
                    "Accept-Language": "en-US",
                    "x-merlin-version": "web-merlin",
                },
            )
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    # -- models ---------------------------------------------------------------

    async def fetch_models(self) -> list[str]:
        """Fetch and cache the model list from the Merlin CDN."""
        if self._models_cache is not None:
            return list(self._models_cache)
        client = self._ensure_client()
        try:
            resp = await client.get(self.settings.merlin_cdn_models_url)
        except httpx.HTTPError as exc:
            logger.warning("Merlin model list fetch failed: %s", exc)
            self._models_cache = self._fallback_models()
            return list(self._models_cache)
        if resp.status_code >= 400:
            logger.warning("Merlin model list fetch returned HTTP %s", resp.status_code)
            self._models_cache = self._fallback_models()
            return list(self._models_cache)
        try:
            payload = resp.json()
        except ValueError:
            self._models_cache = self._fallback_models()
            return list(self._models_cache)
        models = _extract_model_ids(payload)
        if not models:
            models = self._fallback_models()
        self._models_cache = models
        return list(models)

    @staticmethod
    def _fallback_models() -> list[str]:
        return [
            "gemini-2.5-flash-lite",
            "gemini-2.5-pro",
            "gpt-5.5",
            "claude-4.5-haiku",
            "deepseek-v4-pro",
        ]

    def list_models(self) -> list[str]:
        if self._models_cache is not None:
            return list(self._models_cache)
        return self._fallback_models()

    # -- completion -----------------------------------------------------------

    def _chat_url(self) -> str:
        base = self.settings.merlin_base_url.rstrip("/")
        return f"{base}/arcane/api/v2/thread/unified"

    def _chat_headers(self, id_token: str) -> dict:
        return {
            "Authorization": f"Bearer {id_token}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "x-merlin-version": "web-merlin",
            "x-request-timestamp": _now_iso(),
            "User-Agent": _DEFAULT_UA,
            "Accept-Language": "en-US",
        }

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        model: str | None = None,
        *,
        api_key: str | None = None,
    ) -> ProviderResult:
        selected_model = model or self.settings.merlin_default_model
        body = build_merlin_request(messages, selected_model)

        text, finish_reason = await self._do_request(messages, body, selected_model, retry_on_auth=True)
        return ProviderResult(text=text, model=selected_model, finish_reason=finish_reason)

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        model: str | None = None,
        *,
        api_key: str | None = None,
    ) -> AsyncIterator[StreamDelta]:
        selected_model = model or self.settings.merlin_default_model
        body = build_merlin_request(messages, selected_model)

        async for delta in self._stream_request(messages, body, selected_model, retry_on_auth=True):
            yield delta

    async def _do_request(
        self,
        messages: Sequence[ChatMessage],
        body: dict,
        model: str,
        *,
        retry_on_auth: bool,
    ) -> tuple[str, str]:
        """Issue a (buffered) chat request and return (text, finish_reason)."""
        chunks: list[str] = []
        finish_reason = "stop"
        async for delta in self._stream_request(messages, body, model, retry_on_auth=retry_on_auth):
            if delta.text:
                chunks.append(delta.text)
            if delta.finish_reason:
                finish_reason = delta.finish_reason
        return "".join(chunks), finish_reason

    async def _stream_request(
        self,
        messages: Sequence[ChatMessage],
        body: dict,
        model: str,
        *,
        retry_on_auth: bool,
    ) -> AsyncIterator[StreamDelta]:
        id_token = await self._auth.get_token()
        client = self._ensure_client()
        url = self._chat_url()
        headers = self._chat_headers(id_token)

        try:
            async with client.stream(
                "POST", url, json=body, headers=headers
            ) as resp:
                if resp.status_code == 401 and retry_on_auth:
                    # Token may have expired server-side even if not locally.
                    await resp.aread()
                    self._auth.invalidate()
                    async for delta in self._stream_request(
                        messages, body, model, retry_on_auth=False
                    ):
                        yield delta
                    return
                if resp.status_code == 429:
                    detail = await _safe_text(resp)
                    raise ProviderRateLimitError(
                        f"Merlin rate limit exceeded (HTTP 429). {detail}".strip()
                    )
                if resp.status_code in (401, 403):
                    detail = await _safe_text(resp)
                    raise ProviderAuthError(
                        f"Merlin rejected credentials (HTTP {resp.status_code}). {detail}".strip()
                    )
                if resp.status_code >= 500:
                    detail = await _safe_text(resp)
                    raise ProviderUnavailableError(
                        f"Merlin upstream error (HTTP {resp.status_code}). {detail}".strip()
                    )
                if resp.status_code >= 400:
                    detail = await _safe_text(resp)
                    raise ProviderBadGatewayError(
                        f"Merlin request failed (HTTP {resp.status_code}). {detail}".strip()
                    )

                async for delta in self._parse_stream(resp, model):
                    yield delta
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(f"Merlin request timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise ProviderUnavailableError(f"Merlin request failed: {exc}") from exc

    async def _parse_stream(self, resp: httpx.Response, model: str) -> AsyncIterator[StreamDelta]:
        """Parse the SSE response into ``StreamDelta`` chunks."""
        debug_count = 0
        async for event_type, raw in iter_sse_events(_line_iter(resp)):
            event = parse_sse_event(raw)
            if event is None:
                continue
            if debug_count < 5:
                logger.debug("merlin sse[%d] event=%s: %r", debug_count, event_type, raw[:300])
                debug_count += 1
            # Merlin DONE: {"status":"system","data":{"eventType":"DONE"}}
            if event_type == "message" and isinstance(event, dict):
                inner = event.get("data")
                if isinstance(inner, dict) and inner.get("eventType") == "DONE":
                    yield StreamDelta(model=model, finish_reason="stop")
                    return
            if event == "[DONE]":
                yield StreamDelta(model=model, finish_reason="stop")
                return
            # Handle Merlin error events
            if event_type == "error" and isinstance(event, dict):
                msg = event.get("message", "Unknown Merlin error")
                raise ProviderBadGatewayError(f"Merlin error: {msg}")
            # Only extract content from "message" events (skip progress, chatTitle, usage, etc.)
            if event_type != "message":
                continue
            text = extract_content(event)
            if text:
                yield StreamDelta(text=text, model=model)
        # Stream ended without an explicit DONE; still signal completion.
        yield StreamDelta(model=model, finish_reason="stop")


async def _safe_text(resp: httpx.Response) -> str:
    try:
        data = await resp.aread()
        return data.decode("utf-8", errors="replace").strip()[:300]
    except Exception:
        return ""


async def _line_iter(resp: httpx.Response) -> AsyncIterator[str]:
    """Yield decoded text lines from an httpx streaming response."""
    buffer = ""
    async for chunk in resp.aiter_text():
        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line + "\n"
    # Flush remaining buffer
    if buffer:
        yield buffer


def _extract_model_ids(payload: object) -> list[str]:
    """Pull model IDs out of the merlin_constants.json shape (best-effort)."""
    found: list[str] = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            mid = node.get("modelId") or node.get("id") or node.get("model")
            name = node.get("name") or node.get("slug")
            candidate = None
            if isinstance(mid, str) and mid:
                candidate = mid
            elif isinstance(name, str) and name:
                candidate = name
            if candidate and candidate not in found:
                found.append(candidate)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)
    return found
