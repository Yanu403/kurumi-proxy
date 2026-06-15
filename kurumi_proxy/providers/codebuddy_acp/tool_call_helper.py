"""
Tool call helper utilities for ACP → OpenAI translation.

Maintains ACP toolCallId → OpenAI index mapping for streaming tool calls.
"""

from typing import Any


class ToolCallIndexMap:
    """
    Maps ACP toolCallId to OpenAI streaming index.
    
    Usage:
        index_map = ToolCallIndexMap()
        idx = index_map.get_or_assign("tool_abc")  # returns 0
        idx = index_map.get_or_assign("tool_def")  # returns 1
        idx = index_map.get_or_assign("tool_abc")  # returns 0 (cached)
    """
    
    def __init__(self):
        self._map: dict[str, int] = {}
        self._next_index = 0
    
    def get_or_assign(self, tool_call_id: str) -> int:
        """Get existing index or assign a new one."""
        if tool_call_id not in self._map:
            self._map[tool_call_id] = self._next_index
            self._next_index += 1
        return self._map[tool_call_id]
    
    def __len__(self) -> int:
        return len(self._map)
    
    @property
    def has_calls(self) -> bool:
        return len(self._map) > 0


def normalize_tool_call_id(raw_id: str | None) -> str:
    """
    Ensure tool_call_id has call_ prefix for OpenAI compatibility.
    """
    if not raw_id:
        return "call_unknown"
    if raw_id.startswith("call_"):
        return raw_id
    return f"call_{raw_id}"
