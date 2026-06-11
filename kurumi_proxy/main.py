import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from kurumi_proxy.config import Settings, get_settings
from kurumi_proxy.models import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CompletionMessage,
    CompletionUsage,
)
from kurumi_proxy.providers.base import ProviderError
from kurumi_proxy.providers.codebuddy import KNOWN_MODELS, CodeBuddyProvider

ProviderFactory = Callable[[Settings], CodeBuddyProvider]

app = FastAPI(title="kurumi-proxy", version="0.1.0")


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    if isinstance(exc.detail, dict) and "error" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"message": str(exc.detail), "type": "http_error"}},
    )


async def get_app_settings() -> Settings:
    return get_settings()


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
        {
            **base_chunk,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant"},
                    "finish_reason": None,
                }
            ],
        }
    )

    if text:
        yield format_sse_event(
            {
                **base_chunk,
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": text},
                        "finish_reason": None,
                    }
                ],
            }
        )

    yield format_sse_event(
        {
            **base_chunk,
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ],
        }
    )
    yield format_sse_event("[DONE]")


async def require_v1_auth(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    settings: Settings = Depends(get_app_settings),
) -> None:
    if not request.url.path.startswith("/v1/"):
        return
    if not settings.kurumi_proxy_api_key:
        return
    expected = f"Bearer {settings.kurumi_proxy_api_key}"
    if authorization != expected:
        raise HTTPException(
            status_code=401,
            detail={"error": {"message": "Missing or invalid bearer token.", "type": "authentication_error"}},
        )


async def get_provider_factory(request: Request) -> ProviderFactory:
    return getattr(request.app.state, "provider_factory", CodeBuddyProvider)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/models", dependencies=[Depends(require_v1_auth)])
async def models(settings: Settings = Depends(get_app_settings)) -> JSONResponse:
    now = int(time.time())
    model_ids = list(dict.fromkeys([settings.codebuddy_model, *KNOWN_MODELS]))
    return JSONResponse(
        {
            "object": "list",
            "data": [
                {
                    "id": model_id,
                    "object": "model",
                    "created": now,
                    "owned_by": "codebuddy",
                    "permission": [
                        {
                            "id": "modelperm-codebuddy",
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
                for model_id in model_ids
            ],
        }
    )


@app.post("/v1/chat/completions", dependencies=[Depends(require_v1_auth)], response_model=None)
async def chat_completions(
    request: ChatCompletionRequest,
    settings: Settings = Depends(get_app_settings),
    provider_factory: ProviderFactory = Depends(get_provider_factory),
) -> ChatCompletionResponse | StreamingResponse:
    provider = provider_factory(settings)
    selected_model = request.model or settings.codebuddy_model

    try:
        result = await provider.complete(request.messages, selected_model)
    except ProviderError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"error": {"message": exc.message, "type": "provider_error"}},
        ) from exc

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())

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

    prompt_text = "\n".join(message.role + ":" + (str(message.content) if message.content else "") for message in request.messages)
    usage = CompletionUsage(
        prompt_tokens=estimate_tokens(prompt_text),
        completion_tokens=estimate_tokens(result.text),
        total_tokens=estimate_tokens(prompt_text) + estimate_tokens(result.text),
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
