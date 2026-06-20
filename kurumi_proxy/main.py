import json
import logging
import time
import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from kurumi_proxy.config import Settings, get_settings
from kurumi_proxy.db import UsageStore
from kurumi_proxy.models import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CompletionMessage,
    CompletionUsage,
)
from kurumi_proxy.providers.base import BaseProvider, ProviderError
from kurumi_proxy.providers.merlin import MerlinProvider
from kurumi_proxy.rtk import RtkStats, preprocess_messages

logger = logging.getLogger(__name__)

# Merlin fallback models shown on /v1/models before the CDN list is fetched.
_MERLIN_FALLBACK_MODELS = [
    "gemini-2.5-flash-lite",
    "gemini-2.5-pro",
    "gpt-5.5",
    "claude-4.5-haiku",
    "deepseek-v4-pro",
]

app = FastAPI(title="kurumi-proxy", version="0.2.0")


# ---------------------------------------------------------------------------
# Exception handler
# ---------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": str(exc.detail), "type": "http_error"}},
    )


# ---------------------------------------------------------------------------
# Dependencies
# ---------------------------------------------------------------------------


async def get_app_settings() -> Settings:
    return get_settings()


def _usage_store_state_key(settings: Settings) -> str:
    return f"usage_store_{settings.kurumi_proxy_db_path}"


async def get_usage_store(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> UsageStore:
    override = getattr(request.app.state, "usage_store", None)
    if override is not None:
        override.init()
        return override
    key = _usage_store_state_key(settings)
    store = getattr(request.app.state, key, None)
    if store is None:
        store = UsageStore(settings)
        setattr(request.app.state, key, store)
    store.init()
    return store


@app.on_event("startup")
async def init_default_store() -> None:
    UsageStore(get_settings()).init()


def _require_bearer(authorization: str | None, settings: Settings) -> None:
    if not settings.kurumi_proxy_api_key:
        return
    expected = f"Bearer {settings.kurumi_proxy_api_key}"
    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing or invalid bearer token.", "type": "authentication_error"}},
        )


async def require_admin_auth(
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_app_settings),
) -> None:
    _require_bearer(authorization, settings)


async def require_v1_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_app_settings),
) -> None:
    if not request.url.path.startswith("/v1/"):
        return
    _require_bearer(authorization, settings)


async def get_provider(request: Request, settings: Settings = Depends(get_app_settings)) -> BaseProvider:
    """Return the active provider (Merlin). Tests can override via ``app.state.provider_factory``."""
    factory = getattr(request.app.state, "provider_factory", MerlinProvider)
    return factory(settings)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    if not text:
        return 0
    return max(1, (len(text) + 3) // 4)


def format_sse_event(payload: dict[str, object] | str) -> str:
    if isinstance(payload, str):
        data = payload
    else:
        data = json.dumps(payload, separators=(",", ":"))
    return f"data: {data}\n\n"


async def stream_chat_completion(
    *,
    completion_id: str,
    created: int,
    model: str,
    text: str,
) -> AsyncIterator[str]:
    base_chunk: dict[str, object] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
    }

    yield format_sse_event(
        {**base_chunk, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    )
    if text:
        yield format_sse_event(
            {**base_chunk, "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]}
        )
    yield format_sse_event(
        {**base_chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    )
    yield format_sse_event("[DONE]")


def usage_from_text(prompt_text: str, completion_text: str = "") -> CompletionUsage:
    prompt_tokens = estimate_tokens(prompt_text)
    completion_tokens = estimate_tokens(completion_text)
    return CompletionUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def prompt_text_for_usage(messages: list) -> str:
    return "\n".join(
        message.role + ":" + (str(message.content) if message.content else "")
        for message in messages
    )


def _rtk_kwargs(stats: RtkStats) -> dict[str, int | None]:
    return {
        "rtk_before_bytes": stats.before_bytes or None,
        "rtk_after_bytes": stats.after_bytes or None,
        "rtk_saved_bytes": stats.saved_bytes or None,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models", dependencies=[Depends(require_v1_auth)])
async def models() -> JSONResponse:
    now = int(time.time())
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": now,
                    "owned_by": "merlin",
                    "permission": [
                        {
                            "id": f"modelperm-{model_id}",
                            "object": "model_permission",
                            "created": now,
                            "allow_create_engine": False,
                            "allow_sampling": True,
                            "allow_logprobs": False,
                            "allow_search_indices": False,
                            "allow_view": True,
                            "allow_fine_tuning": False,
                            "organization": "*",
                            "group": None,
                            "is_blocking": False,
                        }
                    ],
                }
                for model_id in _MERLIN_FALLBACK_MODELS
            ],
        }
    )


@app.get("/admin/usage", dependencies=[Depends(require_admin_auth)])
async def admin_usage(days: int = 7, store: UsageStore = Depends(get_usage_store)) -> dict[str, object]:
    return store.usage_summary(days=days)


@app.get("/admin/quota", dependencies=[Depends(require_admin_auth)])
async def admin_quota(store: UsageStore = Depends(get_usage_store)) -> dict[str, object]:
    return store.quota_summary()


@app.post("/v1/chat/completions", dependencies=[Depends(require_v1_auth)], response_model=None)
async def chat_completions(
    request: ChatCompletionRequest,
    settings: Settings = Depends(get_app_settings),
    provider: BaseProvider = Depends(get_provider),
    store: UsageStore = Depends(get_usage_store),
) -> ChatCompletionResponse | StreamingResponse:
    processed_messages, rtk_stats = preprocess_messages(request.messages, settings)
    prompt_text = prompt_text_for_usage(processed_messages)
    selected_model = request.model or settings.merlin_default_model
    start = time.monotonic()

    try:
        result = await provider.complete(processed_messages, selected_model)
    except ProviderError as exc:
        usage = usage_from_text(prompt_text)
        store.record_usage(
            model=selected_model,
            endpoint="/v1/chat/completions",
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=0,
            total_tokens=usage.prompt_tokens,
            status="error",
            error=exc.message,
            duration_ms=int((time.monotonic() - start) * 1000),
            **_rtk_kwargs(rtk_stats),
        )
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": {"message": exc.message, "type": "provider_error"}},
        ) from exc

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    usage = usage_from_text(prompt_text, result.text)
    store.record_usage(
        model=result.model,
        endpoint="/v1/chat/completions",
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        total_tokens=usage.total_tokens,
        status="success",
        duration_ms=int((time.monotonic() - start) * 1000),
        **_rtk_kwargs(rtk_stats),
    )

    if request.stream:
        return StreamingResponse(
            stream_chat_completion(
                completion_id=completion_id,
                created=created,
                model=result.model,
                text=result.text,
            ),
            media_type="text/event-stream",
        )

    return ChatCompletionResponse(
        id=completion_id,
        created=created,
        model=result.model,
        choices=[
            ChatCompletionChoice(
                message=CompletionMessage(content=result.text),
                finish_reason="stop",
            )
        ],
        usage=usage,
    )
