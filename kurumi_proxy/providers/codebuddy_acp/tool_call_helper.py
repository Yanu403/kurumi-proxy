"""
Tool call helper for accumulating and normalizing streaming tool calls.

Handles partial JSON arguments across multiple SSE chunks and ensures
stable tool_call IDs for OpenAI compatibility.
"""

import json
import hashlib
from typing import Any


def normalize_tool_call_id(raw_id: str | None, index: int, function_name: str | None = None) -> str:
    """
    Generate a stable tool call ID for OpenAI compatibility.
    
    OpenAI tool_call IDs have format: call_<base64-like-chars>
    We generate a deterministic ID based on index and function name.
    """
    if raw_id and raw_id.startswith("call_"):
        return raw_id
    
    # Generate stable ID from index and function name
    seed = f"{index}:{function_name or 'unknown'}"
    hash_bytes = hashlib.sha256(seed.encode()).digest()[:6]
    hash_str = hash_bytes.hex()
    return f"call_{hash_str}"


class ToolCallAccumulator:
    """
    Accumulates streaming tool call deltas into complete tool calls.
    
    Handles:
    - Partial JSON argument accumulation
    - ID normalization
    - Delta merging across multiple chunks
    """
    
    def __init__(self):
        # Map: tool_call_index -> accumulated data
        self.calls: dict[int, dict[str, Any]] = {}
        self.next_index = 0
    
    def process_delta(self, index: int | None, delta: dict[str, Any]) -> dict[str, Any]:
        """
        Process a tool call delta and return the accumulated state.
        
        Args:
            index: Tool call index (or None to auto-assign)
            delta: Delta object with optional id, type, function fields
        
        Returns:
            Accumulated tool call data with id, type, function
        """
        if index is None:
            index = self.next_index
            self.next_index += 1
        
        # Initialize if new
        if index not in self.calls:
            self.calls[index] = {
                "index": index,
                "id": None,
                "type": "function",
                "function": {
                    "name": None,
                    "arguments": ""
                }
            }
        
        call = self.calls[index]
        
        # Update ID if provided
        if "id" in delta and delta["id"]:
            call["id"] = delta["id"]
        
        # Update type if provided
        if "type" in delta and delta["type"]:
            call["type"] = delta["type"]
        
        # Update function data
        if "function" in delta and delta["function"]:
            func_delta = delta["function"]
            if "name" in func_delta and func_delta["name"]:
                call["function"]["name"] = func_delta["name"]
            if "arguments" in func_delta:
                call["function"]["arguments"] += func_delta["arguments"]
        
        # Normalize ID if we have function name
        if not call["id"] or not call["id"].startswith("call_"):
            call["id"] = normalize_tool_call_id(
                call["id"],
                index,
                call["function"]["name"]
            )
        
        return call
    
    def get_all_calls(self) -> list[dict[str, Any]]:
        """Return all accumulated tool calls as a list."""
        return [self.calls[idx] for idx in sorted(self.calls.keys())]
    
    def validate_call(self, call: dict[str, Any]) -> bool:
        """
        Validate that a tool call is complete and has valid JSON arguments.
        
        Returns True if valid, False otherwise.
        """
        if not call.get("id") or not call.get("function", {}).get("name"):
            return False
        
        args = call.get("function", {}).get("arguments", "")
        if not args.strip():
            # Empty arguments are valid (represents {})
            return True
        
        try:
            json.loads(args)
            return True
        except json.JSONDecodeError:
            return False
