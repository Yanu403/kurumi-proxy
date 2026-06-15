"""
High-level ACP session helpers.

Wraps the low-level AcpJsonRpcClient with session lifecycle methods:
- new() - create a session (session/new)
- prompt() - submit a prompt and stream events (session/prompt)
- close() - best-effort session cancel (session/cancel)
"""

import logging
from typing import Any, AsyncIterator, Optional

from kurumi_proxy.providers.codebuddy_acp.client import AcpJsonRpcClient

logger = logging.getLogger(__name__)


class AcpAuthenticationRequiredError(Exception):
    """Raised when session/new returns auth-required error."""
    pass


class AcpProtocolError(Exception):
    """Raised on JSON-RPC protocol errors."""
    pass


class AcpUpstreamRefusalError(Exception):
    """
    Raised when session/prompt returns stopReason="refusal" with an
    upstream error message in _meta.codebuddy.ai/errorMessage.
    """
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class AcpSession:
    """
    High-level session manager for ACP.
    
    Usage:
        session = AcpSession(rpc_client)
        session_id = await session.new(cwd="/tmp")
        async for event in session.prompt(session_id, prompt_blocks):
            # process event
        await session.close(session_id)
    """
    
    def __init__(self, rpc_client: AcpJsonRpcClient):
        self.client = rpc_client
    
    async def new(
        self,
        *,
        cwd: str = "/tmp",
        mcp_servers: Optional[list[dict[str, Any]]] = None,
    ) -> str:
        """
        Create a new ACP session.
        
        Args:
            cwd: Working directory for the session
            mcp_servers: Optional list of MCP server configs
        
        Returns:
            sessionId string
        
        Raises:
            AcpAuthenticationRequiredError: if daemon is not authenticated
            AcpProtocolError: on other JSON-RPC errors
        """
        params = {
            "cwd": cwd,
            "mcpServers": mcp_servers or [],
        }
        
        logger.debug(f"Creating new ACP session (cwd={cwd})")
        
        async for event in self.client.call("session/new", params):
            # Skip session/update notifications (config options, etc.)
            if event.get("method") == "session/update":
                continue
            
            # Check for result
            if "result" in event:
                result = event["result"]
                session_id = result.get("sessionId")
                if not session_id:
                    raise AcpProtocolError(f"session/new result missing sessionId: {result}")
                logger.info(f"Session created: {session_id}")
                return session_id
            
            # Check for error
            if "error" in event:
                error = event["error"]
                error_data = error.get("data", {})
                
                # Check for auth-required error
                if isinstance(error_data, dict) and error_data.get("category") == "auth":
                    raise AcpAuthenticationRequiredError(
                        "CodeBuddy ACP daemon is not authenticated. "
                        "Run `codebuddy -p \"hi\"` once with CODEBUDDY_API_KEY set, "
                        "then restart Kurumi Proxy."
                    )
                
                raise AcpProtocolError(f"session/new error: {error}")
        
        raise AcpProtocolError("session/new did not return result")
    
    async def prompt(
        self,
        session_id: str,
        prompt_blocks: list[dict[str, Any]],
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Submit a prompt and stream all events (notifications + result).
        
        Args:
            session_id: Session ID from new()
            prompt_blocks: List of prompt blocks, e.g.,
                [{"type": "text", "text": "Hello"}]
        
        Yields:
            All SSE events: session/update notifications and the final
            result event with stopReason.
        """
        params = {
            "sessionId": session_id,
            "prompt": prompt_blocks,
        }
        
        logger.debug(f"Submitting prompt to session {session_id}")
        
        async for event in self.client.call("session/prompt", params):
            yield event
    
    async def close(self, session_id: str) -> None:
        """
        Best-effort session cancel (session/cancel).
        
        Silently ignores errors since this is cleanup.
        """
        try:
            async for event in self.client.call(
                "session/cancel",
                {"sessionId": session_id},
                timeout=5.0,
            ):
                # Drain the stream
                pass
        except Exception as exc:
            logger.debug(f"session/cancel failed (ignored): {exc}")
