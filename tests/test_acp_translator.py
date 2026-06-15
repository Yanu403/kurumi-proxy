"""
Unit tests for ACP translator module.

Tests SSE parsing, event translation, and OpenAI format generation.
"""

import json
import pytest
from typing import AsyncIterator

from kurumi_proxy.providers.codebuddy_acp.translator import (
    translate_to_openai_stream,
    collect_openai_completion,
    extract_content_text,
    extract_tool_call,
    map_stop_reason,
    check_upstream_refusal,
    parse_sse_buffer,
)
from kurumi_proxy.providers.codebuddy_acp.session import AcpUpstreamRefusalError


async def async_list(items):
    """Convert list to async iterator."""
    for item in items:
        yield item


def test_parse_sse_buffer_basic():
    """Test basic SSE buffer parsing."""
    buf = b":ok\n\nevent: message\ndata: {\"test\":\"value\"}\n\n"
    events, remaining = parse_sse_buffer(buf)
    
    assert len(events) == 1
    assert events[0] == {"test": "value"}
    assert remaining == b""


def test_parse_sse_buffer_multiple():
    """Test parsing multiple SSE events."""
    buf = b"data: {\"a\":1}\n\ndata: {\"b\":2}\n\n"
    events, remaining = parse_sse_buffer(buf)
    
    assert len(events) == 2
    assert events[0] == {"a": 1}
    assert events[1] == {"b": 2}


def test_parse_sse_buffer_incomplete():
    """Test parsing incomplete SSE buffer."""
    buf = b"data: {\"test\":\"value\"}\n\nincomplete"
    events, remaining = parse_sse_buffer(buf)
    
    assert len(events) == 1
    assert remaining == b"incomplete"


def test_parse_sse_buffer_comment():
    """Test SSE comment handling."""
    buf = b":ok\n\ndata: {\"test\":\"value\"}\n\n"
    events, remaining = parse_sse_buffer(buf)
    
    assert len(events) == 1
    assert events[0] == {"test": "value"}


def test_extract_content_text_with_content_object():
    """Test extracting text from content object."""
    update = {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "Hello world"}
    }
    
    text = extract_content_text(update)
    assert text == "Hello world"


def test_extract_content_text_with_text_field():
    """Test extracting text from top-level text field."""
    update = {
        "sessionUpdate": "agent_message_chunk",
        "text": "Hello world"
    }
    
    text = extract_content_text(update)
    assert text == "Hello world"


def test_extract_content_text_missing():
    """Test extracting text when missing."""
    update = {"sessionUpdate": "agent_message_chunk"}
    
    text = extract_content_text(update)
    assert text is None


def test_extract_tool_call():
    """Test extracting tool call from tool_call event."""
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "tool_123",
        "toolName": "get_weather",
        "arguments": "{\"city\":\"Tokyo\"}"
    }
    
    tool_call = extract_tool_call(update)
    
    assert tool_call is not None
    assert tool_call["id"] == "call_tool_123"
    assert tool_call["type"] == "function"
    assert tool_call["function"]["name"] == "get_weather"
    assert tool_call["function"]["arguments"] == "{\"city\":\"Tokyo\"}"


def test_extract_tool_call_already_has_prefix():
    """Test tool call with existing call_ prefix."""
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "call_abc123",
        "toolName": "test_func",
        "arguments": "{}"
    }
    
    tool_call = extract_tool_call(update)
    assert tool_call["id"] == "call_abc123"


def test_extract_tool_call_wrong_type():
    """Test extracting tool call from wrong event type."""
    update = {"sessionUpdate": "agent_message_chunk"}
    
    tool_call = extract_tool_call(update)
    assert tool_call is None


def test_map_stop_reason():
    """Test stop reason mapping."""
    assert map_stop_reason("end_turn") == "stop"
    assert map_stop_reason("max_tokens") == "length"
    assert map_stop_reason("cancelled") == "stop"
    assert map_stop_reason("refusal") == "stop"
    assert map_stop_reason("tool_use") == "tool_calls"
    assert map_stop_reason("unknown") == "stop"
    assert map_stop_reason(None) == "stop"


def test_check_upstream_refusal_with_error():
    """Test detecting upstream refusal with error message."""
    result = {
        "stopReason": "refusal",
        "_meta": {
            "codebuddy.ai/errorMessage": "{\"code\":-32000,\"message\":\"Authentication required\"}"
        }
    }
    
    error_msg = check_upstream_refusal(result)
    assert error_msg == "Authentication required"


def test_check_upstream_refusal_plain_string():
    """Test upstream refusal with plain string message."""
    result = {
        "stopReason": "refusal",
        "_meta": {
            "codebuddy.ai/errorMessage": "Some error message"
        }
    }
    
    error_msg = check_upstream_refusal(result)
    assert error_msg == "Some error message"


def test_check_upstream_refusal_not_refusal():
    """Test non-refusal stop reason."""
    result = {"stopReason": "end_turn"}
    
    error_msg = check_upstream_refusal(result)
    assert error_msg is None


def test_check_upstream_refusal_no_meta():
    """Test refusal without _meta."""
    result = {"stopReason": "refusal"}
    
    error_msg = check_upstream_refusal(result)
    assert error_msg is None


@pytest.mark.asyncio
async def test_translate_simple_text_stream():
    """Test streaming translation of simple text response."""
    acp_events = [
        {
            "method": "session/update",
            "params": {
                "sessionId": "sess_123",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Hello, "}
                }
            }
        },
        {
            "method": "session/update",
            "params": {
                "sessionId": "sess_123",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "world!"}
                }
            }
        },
        {
            "result": {
                "stopReason": "end_turn"
            }
        }
    ]
    
    chunks = []
    async for chunk_str in translate_to_openai_stream(
        async_list(acp_events),
        model="test-model",
        completion_id="test-123",
        created=1234567890,
    ):
        chunks.append(chunk_str)
    
    # Should have: initial role chunk + 2 content chunks + finish chunk + [DONE]
    assert len(chunks) >= 4
    assert "Hello, " in chunks[1]
    assert "world!" in chunks[2]
    assert "[DONE]" in chunks[-1]


@pytest.mark.asyncio
async def test_translate_tool_call_stream():
    """Test streaming translation with tool calls."""
    acp_events = [
        {
            "method": "session/update",
            "params": {
                "sessionId": "sess_123",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call_abc123",
                    "toolName": "get_weather",
                    "arguments": "{\"city\":\"Tokyo\"}"
                }
            }
        },
        {
            "result": {
                "stopReason": "tool_use"
            }
        }
    ]
    
    chunks = []
    async for chunk_str in translate_to_openai_stream(
        async_list(acp_events),
        model="test-model",
    ):
        chunks.append(chunk_str)
    
    full_output = "".join(chunks)
    assert "tool_calls" in full_output
    assert "get_weather" in full_output
    assert "call_abc123" in full_output


@pytest.mark.asyncio
async def test_translate_upstream_refusal_raises():
    """Test that upstream refusal raises AcpUpstreamRefusalError."""
    acp_events = [
        {
            "result": {
                "stopReason": "refusal",
                "_meta": {
                    "codebuddy.ai/errorMessage": "400 model [codewise-chat] service info not found"
                }
            }
        }
    ]
    
    with pytest.raises(AcpUpstreamRefusalError, match="codewise-chat"):
        async for _ in translate_to_openai_stream(
            async_list(acp_events),
            model="test-model",
        ):
            pass


@pytest.mark.asyncio
async def test_collect_completion_simple():
    """Test non-streaming collection of simple text response."""
    acp_events = [
        {
            "method": "session/update",
            "params": {
                "sessionId": "sess_123",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Test response"}
                }
            }
        },
        {
            "result": {
                "stopReason": "end_turn"
            }
        }
    ]
    
    response = await collect_openai_completion(
        async_list(acp_events),
        model="test-model",
        completion_id="test-456",
        created=1234567890,
    )
    
    assert response["id"] == "test-456"
    assert response["model"] == "test-model"
    assert response["object"] == "chat.completion"
    assert len(response["choices"]) == 1
    
    choice = response["choices"][0]
    assert choice["message"]["role"] == "assistant"
    assert choice["message"]["content"] == "Test response"
    assert choice["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_collect_completion_with_reasoning():
    """Test non-streaming collection with reasoning content."""
    acp_events = [
        {
            "method": "session/update",
            "params": {
                "sessionId": "sess_123",
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": {"type": "text", "text": "Let me think..."}
                }
            }
        },
        {
            "method": "session/update",
            "params": {
                "sessionId": "sess_123",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Final answer"}
                }
            }
        },
        {
            "result": {
                "stopReason": "end_turn"
            }
        }
    ]
    
    response = await collect_openai_completion(
        async_list(acp_events),
        model="test-model",
    )
    
    choice = response["choices"][0]
    assert choice["message"]["content"] == "Final answer"
    assert choice["message"]["reasoning_content"] == "Let me think..."


@pytest.mark.asyncio
async def test_collect_completion_with_tool_calls():
    """Test non-streaming collection with tool calls."""
    acp_events = [
        {
            "method": "session/update",
            "params": {
                "sessionId": "sess_123",
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "call_xyz789",
                    "toolName": "calculate",
                    "arguments": "{\"expression\":\"2 + 2\"}"
                }
            }
        },
        {
            "result": {
                "stopReason": "tool_use"
            }
        }
    ]
    
    response = await collect_openai_completion(
        async_list(acp_events),
        model="test-model",
    )
    
    choice = response["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    
    assert "tool_calls" in choice["message"]
    tool_calls = choice["message"]["tool_calls"]
    assert len(tool_calls) == 1
    
    tool_call = tool_calls[0]
    assert tool_call["id"] == "call_xyz789"
    assert tool_call["type"] == "function"
    assert tool_call["function"]["name"] == "calculate"
    assert "2 + 2" in tool_call["function"]["arguments"]


@pytest.mark.asyncio
async def test_collect_completion_upstream_refusal_raises():
    """Test that upstream refusal raises AcpUpstreamRefusalError."""
    acp_events = [
        {
            "result": {
                "stopReason": "refusal",
                "_meta": {
                    "codebuddy.ai/errorMessage": "Upstream error message"
                }
            }
        }
    ]
    
    with pytest.raises(AcpUpstreamRefusalError, match="Upstream error"):
        await collect_openai_completion(
            async_list(acp_events),
            model="test-model",
        )
