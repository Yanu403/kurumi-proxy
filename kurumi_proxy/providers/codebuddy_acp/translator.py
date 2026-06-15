"""
ACP events → OpenAI chat completion translator.

Maps real ACP session/update events to OpenAI-compatible format:
- agent_message_chunk (content.text) → delta.content
- agent_thought_chunk (content.text) → delta.reasoning_content
- tool_call (toolCallId, toolName, arguments) → delta.tool_calls
- stopReason → finish_reason

Handles refusal with upstream error surfacing (HTTP 502).
"""

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from kurumi_proxy.providers.codebuddy_acp.session import AcpUpstreamRefusalError

logger = logging.getLogger(__name__)


def parse_sse_buffer(buf: bytes) -> tuple[list[dict[str, Any]], bytes]:
    """
    Parse SSE events from a buffer, returning (events, remaining_buf).
    
    SSE format:
        :ok\n
        event: message\n
        data: {...}\n
        \n
    
    Returns tuple of (parsed_events, leftover_bytes).
    """
    events = []
    while b"\n\n" in buf:
        block, buf = buf.split(b"\n\n", 1)
        data_lines = []
        for line in block.split(b"\n"):
            if line.startswith(b":"):
                continue  # SSE comment
            if line.startswith(b"data:"):
                data_lines.append(line[5:].lstrip().decode())
        if data_lines:
            try:
                events.append(json.loads("".join(data_lines)))
            except json.JSONDecodeError:
                continue
    return events, buf


def extract_content_text(update: dict[str, Any]) -> str | None:
    """
    Extract text from agent_message_chunk or agent_thought_chunk.
    
    Real format: {"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"..."}}
    """
    content = update.get("content")
    if isinstance(content, dict):
        return content.get("text")
    # Fallback: some events might have text at top level
    return update.get("text")


def extract_tool_call(update: dict[str, Any]) -> dict[str, Any] | None:
    """
    Extract tool call from tool_call event.
    
    Real format: {"sessionUpdate":"tool_call","toolCallId":"...","toolName":"...","arguments":"{...}"}
    
    Returns OpenAI-shaped tool_call dict with id, type, function.name, function.arguments.
    """
    if update.get("sessionUpdate") != "tool_call":
        return None
    
    tool_call_id = update.get("toolCallId", "")
    tool_name = update.get("toolName", "")
    arguments = update.get("arguments", "")
    
    # Ensure tool_call_id has call_ prefix for OpenAI compatibility
    if tool_call_id and not tool_call_id.startswith("call_"):
        tool_call_id = f"call_{tool_call_id}"
    
    return {
        "id": tool_call_id,
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments),
        }
    }


def map_stop_reason(stop_reason: str | None, meta: dict | None = None) -> str:
    """
    Map ACP stopReason to OpenAI finish_reason.
    
    - end_turn → stop
    - max_tokens → length
    - cancelled → stop
    - refusal → stop (but caller should check for upstream error)
    - tool_use → tool_calls (if tool_calls were emitted)
    """
    if stop_reason == "end_turn":
        return "stop"
    elif stop_reason == "max_tokens":
        return "length"
    elif stop_reason == "cancelled":
        return "stop"
    elif stop_reason == "refusal":
        return "stop"  # Caller checks _meta for upstream error
    elif stop_reason == "tool_use":
        return "tool_calls"
    else:
        return "stop"


def check_upstream_refusal(result: dict[str, Any]) -> str | None:
    """
    Check if result indicates an upstream refusal with error message.
    
    Returns the error message string if present, otherwise None.
    """
    if result.get("stopReason") != "refusal":
        return None
    
    meta = result.get("_meta", {})
    if not isinstance(meta, dict):
        return None
    
    error_message = meta.get("codebuddy.ai/errorMessage")
    if error_message:
        # error_message might be a JSON string or already parsed.
        # Real shape from upstream:
        #   {"code":-32603,"message":"Internal error",
        #    "data":{"details":"<actual human message>","statusCode":400,...}}
        # Prefer data.details (the human-readable upstream error) over
        # the generic outer "Internal error".
        if isinstance(error_message, str):
            try:
                parsed = json.loads(error_message)
            except json.JSONDecodeError:
                return error_message
            if isinstance(parsed, dict):
                data = parsed.get("data")
                if isinstance(data, dict):
                    details = data.get("details")
                    if isinstance(details, str) and details:
                        return details
                msg = parsed.get("message")
                if isinstance(msg, str) and msg:
                    return msg
                return error_message
            return error_message
        return str(error_message)
    
    return None


async def translate_to_openai_stream(
    acp_events: AsyncIterator[dict[str, Any]],
    *,
    model: str,
    completion_id: str | None = None,
    created: int | None = None,
) -> AsyncIterator[str]:
    """
    Translate ACP stream events to OpenAI SSE format.
    
    Yields SSE-formatted strings: "data: {...}\n\n" and "data: [DONE]\n\n".
    
    Raises AcpUpstreamRefusalError if stopReason="refusal" with upstream error.
    """
    if completion_id is None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    if created is None:
        created = int(time.time())
    
    # Emit initial role chunk
    yield _format_stream_chunk(
        completion_id=completion_id,
        created=created,
        model=model,
        delta={"role": "assistant"},
        finish_reason=None,
    )
    
    # Track tool calls for index mapping
    tool_call_indices: dict[str, int] = {}  # ACP toolCallId → OpenAI index
    next_tool_index = 0
    has_tool_calls = False
    
    async for event in acp_events:
        # Handle session/update notifications
        if event.get("method") == "session/update":
            params = event.get("params", {})
            update = params.get("update", {})
            update_type = update.get("sessionUpdate")
            
            if update_type == "agent_message_chunk":
                text = extract_content_text(update)
                if text:
                    yield _format_stream_chunk(
                        completion_id=completion_id,
                        created=created,
                        model=model,
                        delta={"content": text},
                        finish_reason=None,
                    )
            
            elif update_type == "agent_thought_chunk":
                text = extract_content_text(update)
                if text:
                    yield _format_stream_chunk(
                        completion_id=completion_id,
                        created=created,
                        model=model,
                        delta={"reasoning_content": text},
                        finish_reason=None,
                    )
            
            elif update_type == "tool_call":
                tool_call = extract_tool_call(update)
                if tool_call:
                    has_tool_calls = True
                    tool_id = tool_call["id"]
                    
                    # Assign index for this tool call
                    if tool_id not in tool_call_indices:
                        tool_call_indices[tool_id] = next_tool_index
                        next_tool_index += 1
                    
                    index = tool_call_indices[tool_id]
                    
                    yield _format_stream_chunk(
                        completion_id=completion_id,
                        created=created,
                        model=model,
                        delta={
                            "tool_calls": [{
                                "index": index,
                                "id": tool_call["id"],
                                "type": "function",
                                "function": tool_call["function"],
                            }]
                        },
                        finish_reason=None,
                    )
            
            # Ignore: session_info_update, usage_update, tool_call_update
            
            continue
        
        # Handle final result event
        if "result" in event:
            result = event["result"]
            stop_reason = result.get("stopReason")
            meta = result.get("_meta")
            
            # Check for upstream refusal
            error_message = check_upstream_refusal(result)
            if error_message:
                raise AcpUpstreamRefusalError(error_message)
            
            # Determine finish reason
            finish_reason = map_stop_reason(stop_reason, meta)
            if has_tool_calls and stop_reason in ("tool_use", "end_turn"):
                finish_reason = "tool_calls"
            
            yield _format_stream_chunk(
                completion_id=completion_id,
                created=created,
                model=model,
                delta={},
                finish_reason=finish_reason,
            )
            break
        
        # Handle error event
        if "error" in event:
            error = event["error"]
            logger.error(f"ACP stream error: {error}")
            yield _format_stream_chunk(
                completion_id=completion_id,
                created=created,
                model=model,
                delta={},
                finish_reason="stop",
            )
            break
    
    # Emit [DONE]
    yield "data: [DONE]\n\n"


def _format_stream_chunk(
    *,
    completion_id: str,
    created: int,
    model: str,
    delta: dict[str, Any],
    finish_reason: str | None,
) -> str:
    """Format a single SSE stream chunk."""
    chunk = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "delta": delta,
            "finish_reason": finish_reason,
        }]
    }
    return f"data: {json.dumps(chunk)}\n\n"


async def collect_openai_completion(
    acp_events: AsyncIterator[dict[str, Any]],
    *,
    model: str,
    completion_id: str | None = None,
    created: int | None = None,
) -> dict[str, Any]:
    """
    Collect all ACP events and return a complete OpenAI ChatCompletion dict.
    
    Raises AcpUpstreamRefusalError if stopReason="refusal" with upstream error.
    """
    if completion_id is None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    if created is None:
        created = int(time.time())
    
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls_list: list[dict[str, Any]] = []
    tool_call_map: dict[str, int] = {}  # ACP toolCallId → index
    finish_reason = "stop"
    
    async for event in acp_events:
        # Handle session/update notifications
        if event.get("method") == "session/update":
            params = event.get("params", {})
            update = params.get("update", {})
            update_type = update.get("sessionUpdate")
            
            if update_type == "agent_message_chunk":
                text = extract_content_text(update)
                if text:
                    content_parts.append(text)
            
            elif update_type == "agent_thought_chunk":
                text = extract_content_text(update)
                if text:
                    reasoning_parts.append(text)
            
            elif update_type == "tool_call":
                tool_call = extract_tool_call(update)
                if tool_call:
                    tool_id = tool_call["id"]
                    if tool_id not in tool_call_map:
                        tool_call_map[tool_id] = len(tool_calls_list)
                        tool_calls_list.append(tool_call)
        
        # Handle final result
        if "result" in event:
            result = event["result"]
            stop_reason = result.get("stopReason")
            meta = result.get("_meta")
            
            # Check for upstream refusal
            error_message = check_upstream_refusal(result)
            if error_message:
                raise AcpUpstreamRefusalError(error_message)
            
            finish_reason = map_stop_reason(stop_reason, meta)
            if tool_calls_list and stop_reason in ("tool_use", "end_turn"):
                finish_reason = "tool_calls"
            break
        
        # Handle error
        if "error" in event:
            logger.error(f"ACP error: {event['error']}")
            break
    
    # Build response
    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) if content_parts else None,
    }
    
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    
    if tool_calls_list:
        message["tool_calls"] = tool_calls_list
    
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    }
