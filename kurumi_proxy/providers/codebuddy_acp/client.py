"""
HTTP client for CodeBuddy ACP JSON-RPC protocol.

Handles connection lifecycle, JSON-RPC requests, and SSE response parsing.
"""

import json
import logging
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger(__name__)


class AcpClient:
    """
    Client for CodeBuddy ACP (Agent Client Protocol) over HTTP/SSE.
    
    Protocol flow:
    1. POST /api/v1/acp/connect -> get connectionId
    2. POST /api/v1/acp (with acp-connection-id header) -> JSON-RPC requests
    3. Parse SSE responses (each POST returns its own SSE stream)
    4. DELETE /api/v1/acp -> close connection
    """
    
    def __init__(self, base_url: str):
        self.base_url = base_url
        self.connection_id: str | None = None
        self.session_token: str | None = None
        self._http_client: httpx.AsyncClient | None = None
    
    async def __aenter__(self):
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
    
    async def connect(self) -> None:
        """Establish ACP connection and get connectionId."""
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(timeout=30.0)
        
        logger.debug("Connecting to ACP endpoint")
        response = await self._http_client.post(
            f"{self.base_url}/api/v1/acp/connect",
            headers={"x-codebuddy-request": "1"},
        )
        response.raise_for_status()
        
        data = response.json()
        self.connection_id = data["connectionId"]
        self.session_token = data.get("sessionToken")
        logger.info(f"ACP connected: connectionId={self.connection_id}")
    
    async def close(self) -> None:
        """Close ACP connection and cleanup."""
        if self._http_client is None:
            return
        
        if self.connection_id:
            try:
                await self._http_client.delete(
                    f"{self.base_url}/api/v1/acp",
                    headers=self._acp_headers(),
                )
                logger.debug("ACP connection closed")
            except Exception as exc:
                logger.warning(f"Error closing ACP connection: {exc}")
        
        await self._http_client.aclose()
        self._http_client = None
        self.connection_id = None
    
    def _acp_headers(self) -> dict[str, str]:
        """Build headers for ACP requests."""
        headers = {
            "x-codebuddy-request": "1",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        }
        if self.connection_id:
            headers["acp-connection-id"] = self.connection_id
        return headers
    
    async def json_rpc_request(
        self,
        method: str,
        params: dict[str, Any],
        rpc_id: int | str,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Send JSON-RPC request and yield SSE events.
        
        POST /api/v1/acp returns its own SSE stream with:
        - Notifications: {"jsonrpc":"2.0","method":"session/update","params":{...}}
        - Response: {"jsonrpc":"2.0","id":<rpc_id>,"result":{...}}
        - Error: {"jsonrpc":"2.0","id":<rpc_id>,"error":{...}}
        
        Args:
            method: JSON-RPC method (e.g., "initialize", "session/new", "session/prompt")
            params: JSON-RPC params
            rpc_id: Request ID for correlation
        
        Yields:
            Parsed JSON-RPC messages (notifications and response)
        """
        if self._http_client is None:
            raise RuntimeError("ACP client not connected")
        
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": params,
        }
        
        logger.debug(f"JSON-RPC request: {method} (id={rpc_id})")
        
        async with self._http_client.stream(
            "POST",
            f"{self.base_url}/api/v1/acp",
            headers=self._acp_headers(),
            json=payload,
        ) as response:
            response.raise_for_status()
            
            async for line in response.aiter_lines():
                # Parse SSE format: "event: message\ndata: {...}\n\n"
                # or just "data: {...}\n"
                line = line.strip()
                
                if not line:
                    continue
                
                if line.startswith(":"):
                    # Comment line, skip
                    continue
                
                if line.startswith("event:"):
                    # Event type line, skip (we only care about data)
                    continue
                
                if line.startswith("data:"):
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    
                    try:
                        message = json.loads(data_str)
                        yield message
                    except json.JSONDecodeError as exc:
                        logger.warning(f"Failed to parse SSE data: {exc}")
                        continue
