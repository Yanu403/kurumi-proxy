"""
ACP to OpenAI translator.

Converts ACP session/update events to OpenAI-compatible chat completion
format (both streaming and non-streaming).

Key responsibilities:
- Process agent_message_chunk -> delta.content
- Process agent_thought_chunk -> delta.reasoning_content
- Process tool_call + tool_call_update -> delta.tool_calls
- Handle interruption_request, session_end, usage_update
- Map stopReason to finish_reason
"""

import json
import logging
import time
import uuid
from typing import Any, AsyncIterator

from kurumi_proxy.models import StreamChunk, StreamChoice, DeltaMessage, DeltaToolCall
from kurumi_proxy.providers.codebuddy_acp.tool_call_helper import ToolCallAccumulator

logger = logging.getLogger(__name__)


# ACP stopReason to OpenAI finish_reason mapping
STOP_REASON_MAP = {
    "end_turn": "stop",
    "cancelled": "stop",
    "refusal": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def process_agent_message_chunk(update: dict[str, Any]) -> str | None:
    """Extract text content from agent_message_chunk event."""
    if update.get("sessionUpdate") != "agent_message_chunk":
        return None
    
    # agent_message_chunk has 'text' field
    return update.get("text")


def process_agent_thought_chunk(update: dict[str, Any]) -> str | None:
    """Extract reasoning content from agent_thought_chunk event."""
    if update.get("sessionUpdate") != "agent_thought_chunk":
        return None
    
    # agent_thought_chunk has 'text' field
    return update.get("text")


def process_tool_call_event(update: dict[str, Any]) -> dict[str, Any] | None:
    """
    Process tool_call event (new tool call started).
    
    Returns dict with:
    - index: tool call index
    - id: tool call ID
    - type: "function"
    - function: {"name": "...", "arguments": ""}
    """
    if update.get("sessionUpdate") != "tool_call":
        return None
    
    # tool_call event format:
    # {
    #   "sessionUpdate": "tool_call",
    #   "toolUseId": "...",
    #   "toolName": "...",
    #   "input": {...}  # full input object, not streaming
    # }
    
    tool_use_id = update.get("toolUseId", "")
    tool_name = update.get("toolName", "")
    tool_input = update.get("input", {})
    
    # Convert input to JSON string
    arguments_str = json.dumps(tool_input) if tool_input else ""
    
    return {
        "id": tool_use_id if tool_use_id.startswith("call_") else f"call_{tool_use_id}",
        "type": "function",
        "function": {
            "name": tool_name,
            "arguments": arguments_str,
        }
    }


def process_tool_call_update_event(update: dict[str, Any]) -> dict[str, Any] | None:
    """
    Process tool_call_update event (streaming tool call arguments).
    
    Returns dict with:
    - index: tool call index
    - id: tool call ID (if present)
    - function: {"arguments": "..."}  # incremental
    """
    if update.get("sessionUpdate") != "tool_call_update":
        return None
    
    # tool_call_update event format:
    # {
    #   "sessionUpdate": "tool_call_update",
    #   "toolUseId": "...",
    #   "partialInput": "..."  # partial JSON string
    # }
    
    tool_use_id = update.get("toolUseId")
    partial_input = update.get("partialInput", "")
    
    result = {
        "function": {
            "arguments": partial_input
        }
    }
    
    if tool_use_id:
        result["id"] = tool_use_id if tool_use_id.startswith("call_") else f"call_{tool_use_id}"
    
    return result


def extract_usage_from_result(result: dict[str, Any]) -> dict[str, int] | None:
    """Extract token usage from ACP result event."""
    if "result" not in result:
        return None
    
    result_data = result["result"]
    
    # ACP may include usage in result
    # Check _meta or top-level usage fields
    usage_data = result_data.get("usage") or result_data.get("_meta", {}).get("usage")
    
    if usage_data:
        return {
            "prompt_tokens": usage_data.get("inputTokens", 0),
            "completion_tokens": usage_data.get("outputTokens", 0),
            "total_tokens": usage_data.get("totalTokens", 0),
        }
    
    return None


def extract_stop_reason(result: dict[str, Any]) -> str:
    """Extract and map stopReason from ACP result to OpenAI finish_reason."""
    if "result" not in result:
        return "stop"
    
    stop_reason = result["result"].get("stopReason", "end_turn")
    return STOP_REASON_MAP.get(stop_reason, "stop")


async def translate_to_openai_stream(
    acp_events: AsyncIterator[dict[str, Any]],
    *,
    model: str,
    completion_id: str | None = None,
    created: int | None = None,
) -> AsyncIterator[str]:
    """
    Translate ACP session/update events to OpenAI streaming format.
    
    Yields SSE strings in OpenAI format:
    - "data: {JSON chunk}\\n\\n"
    - "data: [DONE]\\n\\n"
    
    Args:
        acp_events: AsyncIterator of ACP JSON-RPC messages
        model: Model name for response
        completion_id: Optional completion ID (generated if not provided)
        created: Optional created timestamp (current time if not provided)
    
    Yields:
        SSE-formatted strings for OpenAI streaming response
    """
    if completion_id is None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    
    if created is None:
        created = int(time.time())
    
    tool_accumulator = ToolCallAccumulator()
    finish_reason: str | None = None
    has_yielded_content = False
    
    async for message in acp_events:
        # Handle session/update notifications
        if message.get("method") == "session/update":
            params = message.get("params", {})
            update = params.get("update", {})
            
            # Process agent_message_chunk (text content)
            text = process_agent_message_chunk(update)
            if text:
                delta = DeltaMessage(content=text)
                chunk = StreamChunk(
                    id=completion_id,
                    created=created,
                    model=model,
                    choices=[StreamChoice(index=0, delta=delta, finish_reason=None)]
                )
                yield f"data: {chunk.model_dump_json()}\n\n"
                has_yielded_content = True
                continue
            
            # Process agent_thought_chunk (reasoning content)
            thought = process_agent_thought_chunk(update)
            if thought:
                delta = DeltaMessage(reasoning_content=thought)
                chunk = StreamChunk(
                    id=completion_id,
                    created=created,
                    model=model,
                    choices=[StreamChoice(index=0, delta=delta, finish_reason=None)]
                )
                yield f"data: {chunk.model_dump_json()}\n\n"
                has_yielded_content = True
                continue
            
            # Process tool_call (new tool call)
            tool_call_data = process_tool_call_event(update)
            if tool_call_data:
                # Start new tool call
                index = len(tool_accumulator.calls)
                tool_accumulator.process_delta(index, tool_call_data)
                
                # Yield initial tool call delta
                delta_tool_call = DeltaToolCall(
                    index=index,
                    id=tool_call_data["id"],
                    type="function",
                    function={
                        "name": tool_call_data["function"]["name"],
                        "arguments": ""
                    }
                )
                delta = DeltaMessage(tool_calls=[delta_tool_call])
                chunk = StreamChunk(
                    id=completion_id,
                    created=created,
                    model=model,
                    choices=[StreamChoice(index=0, delta=delta, finish_reason=None)]
                )
                yield f"data: {chunk.model_dump_json()}\n\n"
                has_yielded_content = True
                
                # Yield arguments if present
                if tool_call_data["function"]["arguments"]:
                    delta_tool_call = DeltaToolCall(
                        index=index,
                        function={"arguments": tool_call_data["function"]["arguments"]}
                    )
                    delta = DeltaMessage(tool_calls=[delta_tool_call])
                    chunk = StreamChunk(
                        id=completion_id,
                        created=created,
                        model=model,
                        choices=[StreamChoice(index=0, delta=delta, finish_reason=None)]
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"
                continue
            
            # Process tool_call_update (streaming arguments)
            tool_update_data = process_tool_call_update_event(update)
            if tool_update_data:
                # Find the latest tool call index
                index = len(tool_accumulator.calls) - 1
                if index >= 0:
                    tool_accumulator.process_delta(index, tool_update_data)
                    
                    # Yield argument delta
                    delta_tool_call = DeltaToolCall(
                        index=index,
                        function={"arguments": tool_update_data["function"]["arguments"]}
                    )
                    delta = DeltaMessage(tool_calls=[delta_tool_call])
                    chunk = StreamChunk(
                        id=completion_id,
                        created=created,
                        model=model,
                        choices=[StreamChoice(index=0, delta=delta, finish_reason=None)]
                    )
                    yield f"data: {chunk.model_dump_json()}\n\n"
                    has_yielded_content = True
                continue
        
        # Handle result (final response with stopReason)
        if "result" in message:
            finish_reason = extract_stop_reason(message)
            
            # Check for tool calls in result
            if tool_accumulator.calls:
                finish_reason = "tool_calls"
            
            break
        
        # Handle error
        if "error" in message:
            error = message["error"]
            logger.error(f"ACP error: {error}")
            # Treat as refusal/stop
            finish_reason = "stop"
            break
    
    # Yield final chunk with finish_reason
    if not has_yielded_content:
        # No content yielded, send empty delta
        delta = DeltaMessage(content="")
        chunk = StreamChunk(
            id=completion_id,
            created=created,
            model=model,
            choices=[StreamChoice(index=0, delta=delta, finish_reason=finish_reason or "stop")]
        )
        yield f"data: {chunk.model_dump_json()}\n\n"
    else:
        # Send finish chunk
        delta = DeltaMessage()
        chunk = StreamChunk(
            id=completion_id,
            created=created,
            model=model,
            choices=[StreamChoice(index=0, delta=delta, finish_reason=finish_reason or "stop")]
        )
        yield f"data: {chunk.model_dump_json()}\n\n"
    
    # Send [DONE]
    yield "data: [DONE]\n\n"


async def collect_openai_completion(
    acp_events: AsyncIterator[dict[str, Any]],
    *,
    model: str,
    completion_id: str | None = None,
    created: int | None = None,
) -> dict[str, Any]:
    """
    Translate ACP session/update events to OpenAI non-streaming format.
    
    Collects all events and returns a complete ChatCompletionResponse.
    
    Args:
        acp_events: AsyncIterator of ACP JSON-RPC messages
        model: Model name for response
        completion_id: Optional completion ID (generated if not provided)
        created: Optional created timestamp (current time if not provided)
    
    Returns:
        Dict representing ChatCompletionResponse (or ExtendedChatCompletionResponse with tool_calls)
    """
    if completion_id is None:
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
    
    if created is None:
        created = int(time.time())
    
    tool_accumulator = ToolCallAccumulator()
    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    finish_reason = "stop"
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    
    async for message in acp_events:
        # Handle session/update notifications
        if message.get("method") == "session/update":
            params = message.get("params", {})
            update = params.get("update", {})
            
            # Collect agent_message_chunk (text content)
            text = process_agent_message_chunk(update)
            if text:
                content_parts.append(text)
                continue
            
            # Collect agent_thought_chunk (reasoning content)
            thought = process_agent_thought_chunk(update)
            if thought:
                reasoning_parts.append(thought)
                continue
            
            # Collect tool_call
            tool_call_data = process_tool_call_event(update)
            if tool_call_data:
                index = len(tool_accumulator.calls)
                tool_accumulator.process_delta(index, tool_call_data)
                continue
            
            # Collect tool_call_update
            tool_update_data = process_tool_call_update_event(update)
            if tool_update_data:
                index = len(tool_accumulator.calls) - 1
                if index >= 0:
                    tool_accumulator.process_delta(index, tool_update_data)
                continue
            
            # Collect usage_update
            if update.get("sessionUpdate") == "usage_update":
                used = update.get("used", 0)
                size = update.get("size", 0)
                # Rough estimation - ACP doesn't always provide detailed token counts
                usage["completion_tokens"] = max(usage["completion_tokens"], used // 4)
                continue
        
        # Handle result (final response)
        if "result" in message:
            finish_reason = extract_stop_reason(message)
            
            # Extract usage if available
            result_usage = extract_usage_from_result(message)
            if result_usage:
                usage = result_usage
            
            # Check for tool calls
            if tool_accumulator.calls:
                finish_reason = "tool_calls"
            
            break
        
        # Handle error
        if "error" in message:
            error = message["error"]
            logger.error(f"ACP error: {error}")
            finish_reason = "stop"
            break
    
    # Build response
    content = "".join(content_parts) if content_parts else None
    reasoning_content = "".join(reasoning_parts) if reasoning_parts else None
    
    # Build message
    message_data: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    
    # Add tool_calls if present
    if tool_accumulator.calls:
        tool_calls_list = []
        for call in tool_accumulator.get_all_calls():
            tool_calls_list.append({
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["function"]["name"],
                    "arguments": call["function"]["arguments"],
                }
            })
        message_data["tool_calls"] = tool_calls_list
    
    # Add reasoning_content if present
    if reasoning_content:
        message_data["reasoning_content"] = reasoning_content
    
    # Build choice
    choice = {
        "index": 0,
        "message": message_data,
        "finish_reason": finish_reason,
    }
    
    # Build response
    response = {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [choice],
        "usage": usage,
    }
    
    return response
