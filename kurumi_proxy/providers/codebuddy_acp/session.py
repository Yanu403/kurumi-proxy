"""
ACP session management.

Handles JSON-RPC protocol handshake (initialize, session/new) and
prompt submission with SSE event streaming.
"""

import logging
from typing import Any, AsyncIterator

from kurumi_proxy.models import ChatMessage, TextContentBlock
from kurumi_proxy.providers.codebuddy_acp.client import AcpClient

logger = logging.getLogger(__name__)


def message_content_to_acp_prompt(message: ChatMessage) -> list[dict[str, Any]]:
    """
    Convert OpenAI chat message content to ACP prompt blocks.
    
    ACP prompt format:
    [
        {"type": "text", "text": "..."},
        {"type": "image", "data": "...", "mimeType": "..."},
        ...
    ]
    
    Note: type is at top level, not nested under content.
    """
    content = message.content
    
    if content is None:
        return []
    
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    
    # List of content blocks
    blocks = []
    for block in content:
        if isinstance(block, TextContentBlock):
            blocks.append({"type": "text", "text": block.text})
        else:
            # Generic block - try to pass through
            # For now, we only support text blocks
            logger.warning(f"Unsupported content block type: {block.type}")
    
    return blocks


def build_acp_prompt(messages: list[ChatMessage]) -> list[dict[str, Any]]:
    """
    Build ACP prompt from OpenAI messages.
    
    For now, we flatten all messages into text blocks.
    Future: support system messages, multi-turn, etc.
    """
    prompt_blocks = []
    
    for message in messages:
        blocks = message_content_to_acp_prompt(message)
        
        # Add role prefix if not system (ACP handles system separately)
        role_prefix = ""
        if message.role == "user":
            role_prefix = "User: "
        elif message.role == "assistant":
            role_prefix = "Assistant: "
        elif message.role == "system":
            role_prefix = "System: "
        
        # Combine with role prefix
        for block in blocks:
            if block["type"] == "text" and role_prefix:
                block["text"] = role_prefix + block["text"]
            prompt_blocks.append(block)
    
    return prompt_blocks


class AcpSession:
    """
    Manages an ACP session lifecycle.
    
    Flow:
    1. initialize() -> protocol handshake
    2. new_session() -> create session
    3. prompt() -> submit user prompt and stream events
    """
    
    def __init__(self, client: AcpClient):
        self.client = client
        self.session_id: str | None = None
        self._rpc_id = 0
    
    def _next_rpc_id(self) -> int:
        """Get next JSON-RPC request ID."""
        self._rpc_id += 1
        return self._rpc_id
    
    async def initialize(self) -> dict[str, Any]:
        """
        Initialize ACP protocol.
        
        JSON-RPC: {"method": "initialize", "params": {...}}
        
        Returns result with agentCapabilities, protocolVersion, etc.
        """
        logger.debug("Initializing ACP protocol")
        
        params = {
            "protocolVersion": 1,
            "clientCapabilities": {},
        }
        
        result = None
        async for message in self.client.json_rpc_request(
            method="initialize",
            params=params,
            rpc_id=self._next_rpc_id(),
        ):
            if "result" in message:
                result = message["result"]
                logger.info(f"ACP initialized: protocol v{result.get('protocolVersion')}")
                break
            elif "error" in message:
                error = message["error"]
                raise RuntimeError(f"ACP initialize error: {error}")
        
        if result is None:
            raise RuntimeError("ACP initialize did not return result")
        
        return result
    
    async def new_session(self, cwd: str = "/tmp") -> dict[str, Any]:
        """
        Create new ACP session.
        
        JSON-RPC: {"method": "session/new", "params": {"cwd": "...", ...}}
        
        Returns result with sessionId, models, modes, etc.
        """
        logger.debug("Creating new ACP session")
        
        params = {
            "cwd": cwd,
            "mcpServers": [],
        }
        
        result = None
        async for message in self.client.json_rpc_request(
            method="session/new",
            params=params,
            rpc_id=self._next_rpc_id(),
        ):
            # Skip session/update notifications
            if message.get("method") == "session/update":
                continue
            
            if "result" in message:
                result = message["result"]
                self.session_id = result["sessionId"]
                logger.info(f"Session created: {self.session_id}")
                break
            elif "error" in message:
                error = message["error"]
                raise RuntimeError(f"ACP session/new error: {error}")
        
        if result is None:
            raise RuntimeError("ACP session/new did not return result")
        
        return result
    
    async def prompt(
        self,
        messages: list[ChatMessage],
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Submit prompt and stream session/update events.
        
        JSON-RPC: {"method": "session/prompt", "params": {"sessionId": "...", "prompt": [...]}}
        
        Yields:
        - session/update notifications (agent_message_chunk, tool_call, etc.)
        - Final result with stopReason, usage, etc.
        """
        if self.session_id is None:
            raise RuntimeError("No active session - call new_session() first")
        
        logger.debug(f"Submitting prompt to session {self.session_id}")
        
        prompt_blocks = build_acp_prompt(messages)
        
        params = {
            "sessionId": self.session_id,
            "prompt": prompt_blocks,
        }
        
        async for message in self.client.json_rpc_request(
            method="session/prompt",
            params=params,
            rpc_id=self._next_rpc_id(),
        ):
            yield message
