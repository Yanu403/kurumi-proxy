import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from inspect import signature
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from kurumi_proxy.config import Settings, get_settings
from kurumi_proxy.db import Connection, ConnectionStore
from kurumi_proxy.models import (
    ChatCompletionChoice,
    ChatCompletionRequest,
    ChatCompletionResponse,
    CompletionMessage,
    CompletionUsage,
)
from kurumi_proxy.providers.base import ProviderError, ProviderResult
from kurumi_proxy.providers.codebuddy import KNOWN_MODELS, CodeBuddyProvider
from kurumi_proxy.router import CredentialRouter
from kurumi_proxy.rtk import RtkStats, preprocess_messages
from kurumi_proxy.providers.codebuddy_acp.daemon import AcpDaemon
from kurumi_proxy.providers.codebuddy_acp.client import AcpClient
from kurumi_proxy.providers.codebuddy_acp.session import AcpSession
from kurumi_proxy.providers.codebuddy_acp.translator import translate_to_openai_stream, collect_openai_completion

# Global ACP daemon instance (lazily initialized)
_acp_daemon: AcpDaemon | None = None

def get_acp_daemon(settings: Settings) -> AcpDaemon:
    """Get or create the global ACP daemon."""
    global _acp_daemon
    if _acp_daemon is None:
        _acp_daemon = AcpDaemon(
            port=settings.codebuddy_daemon_port,
            codebuddy_bin=settings.codebuddy_bin,
        )
    return _acp_daemon

ProviderFactory = Callable[[Settings], CodeBuddyProvider]

app = FastAPI(title="kurumi-proxy", version="0.1.0")


class ConnectionCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    api_key: str = Field(min_length=1)
    priority: int = 100
    is_active: bool = True


class ConnectionPatchRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1)
    api_key: str | None = Field(default=None, min_length=1)
    priority: int | None = None
    is_active: bool | None = None


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


def _store_state_key(settings: Settings) -> str:
    return f"connection_store_{settings.kurumi_proxy_db_path}"


async def get_connection_store(
    request: Request,
    settings: Settings = Depends(get_app_settings),
) -> ConnectionStore:
    override = getattr(request.app.state, "connection_store", None)
    if override is not None:
        override.init()
        return override
    key = _store_state_key(settings)
    store = getattr(request.app.state, key, None)
    if store is None:
        store = ConnectionStore(settings)
        setattr(request.app.state, key, store)
    store.init()
    return store


@app.on_event("startup")
async def init_default_store() -> None:
    ConnectionStore(get_settings()).init()


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
    _require_bearer(authorization, settings)


async def get_provider_factory(request: Request) -> ProviderFactory:
    return getattr(request.app.state, "provider_factory", CodeBuddyProvider)


async def call_provider(
    provider: CodeBuddyProvider,
    messages: list,
    model: str,
    *,
    api_key: str | None,
) -> ProviderResult:
    complete = provider.complete
    if "api_key" in signature(complete).parameters:
        return await complete(messages, model, api_key=api_key)
    return await complete(messages, model)


def usage_from_text(prompt_text: str, completion_text: str = "") -> CompletionUsage:
    prompt_tokens = estimate_tokens(prompt_text)
    completion_tokens = estimate_tokens(completion_text)
    return CompletionUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )


def prompt_text_for_usage(messages: list) -> str:
    return "\n".join(message.role + ":" + (str(message.content) if message.content else "") for message in messages)


def _rtk_kwargs(stats: RtkStats) -> dict[str, int | None]:
    return {
        "rtk_before_bytes": stats.before_bytes or None,
        "rtk_after_bytes": stats.after_bytes or None,
        "rtk_saved_bytes": stats.saved_bytes or None,
    }


def reject_unsupported_tool_calls(request: ChatCompletionRequest, settings: Settings) -> None:
    # Allow tools when using ACP backend
    if settings.kurumi_proxy_backend == "acp":
        return
    if not request.tools:
        return
    raise HTTPException(
        status_code=400,
        detail={
            "error": {
                "message": "Kurumi Proxy is text-only and does not support tool_calls yet. Remove tools/tool_choice and send a text-only chat completion request.",
                "type": "invalid_request_error",
                "param": "tools",
                "code": "unsupported_tool_calls",
            }
        },
    )


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


@app.get("/admin/connections", dependencies=[Depends(require_admin_auth)])
async def admin_list_connections(store: ConnectionStore = Depends(get_connection_store)) -> dict[str, object]:
    return {"data": [connection.safe_dict() for connection in store.list_connections(include_inactive=True)]}


@app.post("/admin/connections", dependencies=[Depends(require_admin_auth)])
async def admin_create_connection(
    payload: ConnectionCreateRequest,
    store: ConnectionStore = Depends(get_connection_store),
) -> dict[str, object]:
    connection = store.create_connection(
        name=payload.name,
        api_key=payload.api_key,
        priority=payload.priority,
        is_active=payload.is_active,
    )
    return connection.safe_dict()


@app.patch("/admin/connections/{connection_id}", dependencies=[Depends(require_admin_auth)])
async def admin_patch_connection(
    connection_id: str,
    payload: ConnectionPatchRequest,
    store: ConnectionStore = Depends(get_connection_store),
) -> dict[str, object]:
    updates = payload.model_dump(exclude_unset=True)
    connection = store.update_connection(connection_id, **updates)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return connection.safe_dict()


@app.delete("/admin/connections/{connection_id}", dependencies=[Depends(require_admin_auth)])
async def admin_delete_connection(
    connection_id: str,
    store: ConnectionStore = Depends(get_connection_store),
) -> dict[str, object]:
    connection = store.deactivate_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return connection.safe_dict()


@app.post("/admin/connections/{connection_id}/reset", dependencies=[Depends(require_admin_auth)])
async def admin_reset_connection(
    connection_id: str,
    store: ConnectionStore = Depends(get_connection_store),
) -> dict[str, object]:
    connection = store.reset_connection(connection_id)
    if connection is None:
        raise HTTPException(status_code=404, detail="Connection not found")
    return connection.safe_dict()


@app.get("/admin/usage", dependencies=[Depends(require_admin_auth)])
async def admin_usage(days: int = 7, store: ConnectionStore = Depends(get_connection_store)) -> dict[str, object]:
    return store.usage_summary(days=days)


@app.get("/admin/quota", dependencies=[Depends(require_admin_auth)])

@app.get("/admin/acp/status", dependencies=[Depends(require_admin_auth)])
async def admin_acp_status(
    settings: Settings = Depends(get_app_settings),
) -> dict:
    """Get ACP daemon status for monitoring."""
    daemon = get_acp_daemon(settings)
    status = daemon.get_status()
    status["backend"] = settings.kurumi_proxy_backend
    status["daemon_port"] = settings.codebuddy_daemon_port
    return status
async def admin_quota(store: ConnectionStore = Depends(get_connection_store)) -> dict[str, object]:
    return store.quota_summary()



async def chat_completions_acp(
    request: ChatCompletionRequest,
    settings: Settings,
    store: ConnectionStore,
) -> ChatCompletionResponse | StreamingResponse:
    """
    Handle chat completions using ACP backend (persistent daemon).
    
    This function:
    1. Ensures CodeBuddy daemon is running
    2. Creates ACP client and session
    3. Sends prompt and streams/collects responses
    4. Translates ACP events to OpenAI format
    5. Records usage
    """
    daemon = get_acp_daemon(settings)
    selected_model = request.model or settings.codebuddy_model
    
    # Ensure daemon is running
    try:
        await daemon.ensure_running()
    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": str(exc), "type": "daemon_error"}},
        ) from exc
    
    # Preprocess messages (RTK if enabled)
    processed_messages, rtk_stats = preprocess_messages(request.messages, settings)
    
    start = time.monotonic()
    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    
    try:
        # Create ACP client and session
        async with AcpClient(daemon.base_url) as client:
            session = AcpSession(client)
            
            # Initialize protocol
            await session.initialize()
            
            # Create session
            await session.new_session(cwd="/tmp")
            
            # Submit prompt and get event stream
            acp_events = session.prompt(processed_messages)
            
            if request.stream:
                # Streaming response
                async def stream_wrapper():
                    try:
                        async for chunk in translate_to_openai_stream(
                            acp_events,
                            model=selected_model,
                            completion_id=completion_id,
                            created=created,
                        ):
                            yield chunk
                    except Exception as exc:
                        logger.error(f"ACP streaming error: {exc}")
                        error_chunk = {
                            "id": completion_id,
                            "object": "chat.completion.chunk",
                            "created": created,
                            "model": selected_model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {"content": f"\n[Error: {exc}]"},
                                    "finish_reason": "stop"
                                }
                            ]
                        }
                        yield f"data: {json.dumps(error_chunk)}\n\n"
                        yield "data: [DONE]\n\n"
                
                # Record usage (estimate for streaming)
                prompt_text = " ".join(
                    m.content if isinstance(m.content, str) else str(m.content)
                    for m in processed_messages
                )
                usage = usage_from_text(prompt_text)
                store.record_usage(
                    model=selected_model,
                    connection=None,
                    endpoint="/v1/chat/completions",
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=0,  # Unknown for streaming
                    total_tokens=usage.prompt_tokens,
                    status="success",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    **_rtk_kwargs(rtk_stats),
                )
                
                return StreamingResponse(
                    stream_wrapper(),
                    media_type="text/event-stream",
                )
            else:
                # Non-streaming response
                response_data = await collect_openai_completion(
                    acp_events,
                    model=selected_model,
                    completion_id=completion_id,
                    created=created,
                )
                
                # Record usage
                usage = response_data.get("usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
                store.record_usage(
                    model=selected_model,
                    connection=None,
                    endpoint="/v1/chat/completions",
                    prompt_tokens=usage["prompt_tokens"],
                    completion_tokens=usage["completion_tokens"],
                    total_tokens=usage["total_tokens"],
                    status="success",
                    duration_ms=int((time.monotonic() - start) * 1000),
                    **_rtk_kwargs(rtk_stats),
                )
                
                return response_data
    
    except HTTPException:
        raise
    except Exception as exc:
        logger.error(f"ACP request error: {exc}")
        raise HTTPException(
            status_code=502,
            detail={"error": {"message": f"ACP backend error: {exc}", "type": "backend_error"}},
        ) from exc

@app.post("/v1/chat/completions", dependencies=[Depends(require_v1_auth)], response_model=None)
async def chat_completions(
    request: ChatCompletionRequest,
    settings: Settings = Depends(get_app_settings),
    provider_factory: ProviderFactory = Depends(get_provider_factory),
    store: ConnectionStore = Depends(get_connection_store),
) -> ChatCompletionResponse | StreamingResponse:
    # Route to ACP backend if configured
    if settings.kurumi_proxy_backend == "acp":
        return await chat_completions_acp(request, settings, store)
    reject_unsupported_tool_calls(request, settings)

    provider = provider_factory(settings)
    selected_model = request.model or settings.codebuddy_model
    processed_messages, rtk_stats = preprocess_messages(request.messages, settings)
    prompt_text = prompt_text_for_usage(processed_messages)
    start = time.monotonic()

    result: ProviderResult | None = None
    selected_connection: Connection | None = None
    last_error: ProviderError | None = None
    router = CredentialRouter(store, settings)
    attempted: set[str] = set()

    # Keep tests and custom in-process providers ergonomic when no upstream key exists.
    custom_provider_without_credentials = (
        provider_factory is not CodeBuddyProvider
        and not settings.codebuddy_api_key
        and not store.list_connections(include_inactive=False)
    )

    if custom_provider_without_credentials:
        try:
            result = await call_provider(provider, processed_messages, selected_model, api_key=None)
        except ProviderError as exc:
            last_error = exc
            usage = usage_from_text(prompt_text)
            store.record_usage(
                model=selected_model,
                connection=None,
                endpoint="/v1/chat/completions",
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=0,
                total_tokens=usage.prompt_tokens,
                status="error",
                error=exc.message,
                duration_ms=int((time.monotonic() - start) * 1000),
                **_rtk_kwargs(rtk_stats),
            )
    else:
        while True:
            connection = router.next_connection(selected_model, attempted)
            if connection is None:
                if last_error is None:
                    last_error = router.no_credentials_error()
                break
            attempted.add(connection.id)
            try:
                result = await call_provider(provider, processed_messages, selected_model, api_key=connection.api_key)
                selected_connection = connection
                router.mark_success(connection)
                break
            except ProviderError as exc:
                last_error = exc
                usage = usage_from_text(prompt_text)
                store.record_usage(
                    model=selected_model,
                    connection=connection,
                    endpoint="/v1/chat/completions",
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=0,
                    total_tokens=usage.prompt_tokens,
                    status="error",
                    error=exc.message,
                    duration_ms=int((time.monotonic() - start) * 1000),
                    **_rtk_kwargs(rtk_stats),
                )
                classification = router.mark_failure(connection, selected_model, exc)
                if not classification.retryable:
                    break

    if result is None:
        assert last_error is not None
        raise HTTPException(
            status_code=last_error.status_code,
            detail={"error": {"message": last_error.message, "type": "provider_error"}},
        ) from last_error

    completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    created = int(time.time())
    usage = usage_from_text(prompt_text, result.text)
    store.record_usage(
        model=result.model,
        connection=selected_connection,
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
