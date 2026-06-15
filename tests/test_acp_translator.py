"""
Unit tests for ACP translator module.

Tests translation of ACP events to OpenAI format (streaming and non-streaming).
"""

import pytest
from typing import AsyncIterator

from kurumi_proxy.providers.codebuddy_acp.translator import (
    translate_to_openai_stream,
    collect_openai_completion,
    process_agent_message_chunk,
    process_tool_call_event,
    extract_stop_reason,
)


async def async_list(items):
    """Convert list to async iterator."""
    for item in items:
        yield item


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
                    "text": "Hello, "
                }
            }
        },
        {
            "method": "session/update",
            "params": {
                "sessionId": "sess_123",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "text": "world!"
                }
            }
        },
        {
            "result": {
                "stopReason": "end_turn",
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 15,
                    "totalTokens": 25,
                }
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
    
    # Should have at least 3 chunks: 2 content + 1 finish + [DONE]
    assert len(chunks) >= 4
    assert "Hello, " in chunks[0]
    assert "world!" in chunks[1]
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
                    "toolUseId": "call_abc123",
                    "toolName": "get_weather",
                    "input": {"city": "Tokyo"}
                }
            }
        },
        {
            "result": {
                "stopReason": "tool_use",
            }
        }
    ]
    
    chunks = []
    async for chunk_str in translate_to_openai_stream(
        async_list(acp_events),
        model="test-model",
    ):
        chunks.append(chunk_str)
    
    # Should contain tool_calls in chunks
    full_output = "".join(chunks)
    assert "tool_calls" in full_output
    assert "get_weather" in full_output
    assert "call_abc123" in full_output
    assert "finish_reason" in full_output


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
                    "text": "Test response"
                }
            }
        },
        {
            "result": {
                "stopReason": "end_turn",
                "usage": {
                    "inputTokens": 10,
                    "outputTokens": 5,
                    "totalTokens": 15,
                }
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
    
    assert response["usage"]["prompt_tokens"] == 10
    assert response["usage"]["completion_tokens"] == 5
    assert response["usage"]["total_tokens"] == 15


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
                    "toolUseId": "call_xyz789",
                    "toolName": "calculate",
                    "input": {"expression": "2 + 2"}
                }
            }
        },
        {
            "result": {
                "stopReason": "tool_use",
            }
        }
    ]
    
    response = await collect_openai_completion(
        async_list(acp_events),
        model="test-model",
    )
    
    assert len(response["choices"]) == 1
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


def test_process_agent_message_chunk():
    """Test processing of agent_message_chunk events."""
    update = {
        "sessionUpdate": "agent_message_chunk",
        "text": "Hello there"
    }
    
    text = process_agent_message_chunk(update)
    assert text == "Hello there"
    
    # Non-matching update
    update_wrong = {
        "sessionUpdate": "other_event",
        "text": "Should be ignored"
    }
    text = process_agent_message_chunk(update_wrong)
    assert text is None


def test_process_tool_call_event():
    """Test processing of tool_call events."""
    update = {
        "sessionUpdate": "tool_call",
        "toolUseId": "call_test123",
        "toolName": "test_function",
        "input": {"arg1": "value1", "arg2": 42}
    }
    
    tool_data = process_tool_call_event(update)
    assert tool_data is not None
    assert tool_data["id"] == "call_test123"
    assert tool_data["type"] == "function"
    assert tool_data["function"]["name"] == "test_function"
    assert "value1" in tool_data["function"]["arguments"]
    assert "42" in tool_data["function"]["arguments"]


def test_extract_stop_reason():
    """Test stop reason extraction and mapping."""
    # end_turn -> stop
    result = {"result": {"stopReason": "end_turn"}}
    assert extract_stop_reason(result) == "stop"
    
    # cancelled -> stop
    result = {"result": {"stopReason": "cancelled"}}
    assert extract_stop_reason(result) == "stop"
    
    # refusal -> content_filter (upstream auth/safety refusals are surfaced
    # separately as HTTP 502 by the chat handler; the OpenAI-shape finish
    # reason for the underlying choice should reflect the filter, not stop)
    result = {"result": {"stopReason": "refusal"}}
    assert extract_stop_reason(result) == "content_filter"
    
    # max_tokens -> length
    result = {"result": {"stopReason": "max_tokens"}}
    assert extract_stop_reason(result) == "length"
    
    # tool_use -> tool_calls
    result = {"result": {"stopReason": "tool_use"}}
    assert extract_stop_reason(result) == "tool_calls"
    
    # Unknown -> stop (default)
    result = {"result": {"stopReason": "unknown_reason"}}
    assert extract_stop_reason(result) == "stop"
    
    # No result -> stop (default)
    result = {}
    assert extract_stop_reason(result) == "stop"
