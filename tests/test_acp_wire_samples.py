"""
Integration tests using real wire samples from docs/wire-samples/.

These tests verify that the SSE parser and translator correctly handle
actual ACP daemon responses captured from live probes.
"""

import json
import pytest
from pathlib import Path

from kurumi_proxy.providers.codebuddy_acp.translator import (
    parse_sse_buffer,
    collect_openai_completion,
)
from kurumi_proxy.providers.codebuddy_acp.session import AcpUpstreamRefusalError


WIRESAMPLES_DIR = Path(__file__).parent.parent / "docs" / "wire-samples"


def load_wire_sample(filename: str) -> bytes:
    """Load a wire sample file."""
    path = WIRESAMPLES_DIR / filename
    return path.read_bytes()


async def events_from_wire(data: bytes):
    """Parse SSE wire data and yield events."""
    events, _ = parse_sse_buffer(data)
    for event in events:
        yield event


def test_parse_initialize_wire_sample():
    """Test parsing the real initialize.sse wire sample."""
    data = load_wire_sample("02_real_initialize.sse")
    events, remaining = parse_sse_buffer(data)
    
    # Should have one result event
    assert len(events) == 1
    event = events[0]
    
    # Should be a JSON-RPC result
    assert "result" in event
    assert event["jsonrpc"] == "2.0"
    assert event["id"] == 1
    
    result = event["result"]
    assert result["protocolVersion"] == 1
    assert "agentCapabilities" in result
    assert "authMethods" in result


def test_parse_session_new_wire_sample():
    """Test parsing the real session_new_authed.sse wire sample."""
    data = load_wire_sample("03_real_session_new_authed.sse")
    events, remaining = parse_sse_buffer(data)
    
    # Should have multiple events (notifications + result)
    assert len(events) > 1
    
    # Find the result event
    result_event = None
    for event in events:
        if "result" in event:
            result_event = event
            break
    
    assert result_event is not None
    assert "sessionId" in result_event["result"]


@pytest.mark.asyncio
async def test_collect_completion_from_refusal_wire_sample():
    """Test collecting completion from real refusal wire sample."""
    data = load_wire_sample("01_session_prompt_refusal.sse")
    events, _ = parse_sse_buffer(data)
    
    # Should raise AcpUpstreamRefusalError
    with pytest.raises(AcpUpstreamRefusalError) as exc_info:
        await collect_openai_completion(
            events_from_wire(data),
            model="test-model",
        )
    
    # Error message should mention codewise-chat or authentication
    error_msg = exc_info.value.message
    assert "codewise-chat" in error_msg or "Authentication" in error_msg


def test_sse_parser_handles_keepalive_comment():
    """Test that SSE parser correctly handles :ok keep-alive comment."""
    data = b":ok\n\ndata: {\"test\":\"value\"}\n\n"
    events, remaining = parse_sse_buffer(data)
    
    # Should parse the data event, ignore the comment
    assert len(events) == 1
    assert events[0] == {"test": "value"}


def test_sse_parser_handles_multiple_data_lines():
    """Test that SSE parser concatenates multiple data: lines."""
    data = b"data: {\"a\":" b"\ndata: 1}\n\n"
    events, remaining = parse_sse_buffer(data)
    
    # Should concatenate and parse
    assert len(events) == 1
    assert events[0] == {"a": 1}


def test_sse_parser_handles_event_field():
    """Test that SSE parser handles event: field (ignores it)."""
    data = b"event: message\ndata: {\"test\":\"value\"}\n\n"
    events, remaining = parse_sse_buffer(data)
    
    # Should parse the data, ignore event field
    assert len(events) == 1
    assert events[0] == {"test": "value"}
