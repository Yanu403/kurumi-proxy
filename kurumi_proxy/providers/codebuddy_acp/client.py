"""
Low-level HTTP/SSE JSON-RPC client for CodeBuddy ACP.

All JSON-RPC traffic goes to a single endpoint with required headers:
- acp-connection-id
- acp-session-token
- Accept: application/json, text/event-stream

Responses are SSE streams. Each SSE event has `event: message` and
`data: <json>`. The first line is often `:ok` (SSE comment, ignore).
"""

import json
import logging
from typing import Any, AsyncIterator, Optional

import httpx

logger = logging.getLogger(__name__)


class AcpJsonRpcClient:
    """
    Client for CodeBuddy ACP JSON-RPC over HTTP/SSE.
    
    Usage:
        client = AcpJsonRpcClient(base_url, connection_id, session_token)
        async for event in client.call("session/new", {"cwd": "/tmp"}):
            if "result" in event:
                session_id = event["result"]["sessionId"]
                break
    """
    
    def __init__(
        self,
        base_url: str,
        connection_id: str,
        session_token: Optional[str] = None,
        http_client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.connection_id = connection_id
        self.session_token = session_token
        self._http_client = http_client or httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=None, write=10.0, pool=10.0)
        )
        self._owns_client = http_client is None
        self._rpc_id_counter = 0
    
    async def close(self) -> None:
        """Close the HTTP client if we own it."""
        if self._owns_client:
            await self._http_client.aclose()
    
    def _next_rpc_id(self) -> int:
        """Get next auto-incrementing JSON-RPC request ID."""
        self._rpc_id_counter += 1
        return self._rpc_id_counter
    
    def _build_headers(self) -> dict[str, str]:
        """Build required headers for JSON-RPC requests."""
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "acp-connection-id": self.connection_id,
        }
        if self.session_token:
            headers["acp-session-token"] = self.session_token
        return headers
    
    async def call(
        self,
        method: str,
        params: dict[str, Any],
        *,
        timeout: float = 60.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """
        Send a JSON-RPC request and yield parsed SSE events.
        
        Args:
            method: JSON-RPC method name (e.g., "initialize", "session/new")
            params: JSON-RPC params dict
            timeout: Request timeout in seconds (default 60s)
        
        Yields:
            Parsed JSON objects from SSE `data:` lines. Notifications have
            `method` field; the final result has `result` or `error` field
            with matching `id`.
        
        Example:
            async for event in client.call("session/new", {"cwd": "/tmp"}):
                if "result" in event:
                    session_id = event["result"]["sessionId"]
                    break
        """
        rpc_id = self._next_rpc_id()
        payload = {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "method": method,
            "params": params,
        }
        
        logger.debug(f"JSON-RPC call: {method} (id={rpc_id})")
        
        async with self._http_client.stream(
            "POST",
            f"{self.base_url}",
            headers=self._build_headers(),
            json=payload,
            timeout=timeout,
        ) as response:
            response.raise_for_status()
            
            async for event in self._iter_sse(response):
                yield event
    
    async def _iter_sse(self, response: httpx.Response) -> AsyncIterator[dict[str, Any]]:
        """
        Parse SSE stream from response body.
        
        SSE format:
            :ok\n                              # comment, ignore
            event: message\n                   # event type (always "message")
            data: {"jsonrpc":"2.0",...}\n      # JSON payload
            \n                                 # blank line = end of event
        
        Multiple `data:` lines are concatenated before JSON parsing.
        """
        buf = b""
        async for chunk in response.aiter_bytes():
            buf += chunk
            while b"\n\n" in buf:
                block, buf = buf.split(b"\n\n", 1)
                data_lines = []
                for line in block.split(b"\n"):
                    if line.startswith(b":"):
                        continue  # SSE comment, e.g., ":ok"
                    if line.startswith(b"data:"):
                        data_lines.append(line[5:].lstrip().decode())
                if data_lines:
                    try:
                        yield json.loads("".join(data_lines))
                    except json.JSONDecodeError as exc:
                        logger.warning(f"SSE JSON decode error: {exc}")
                        continue
