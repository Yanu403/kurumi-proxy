"""
Fake CodeBuddy serve daemon for hermetic testing.

Mimics the HTTP/SSE behavior of `codebuddy --serve` without requiring
a real daemon or valid CODEBUDDY_API_KEY.
"""

import asyncio
import json
import uuid
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import StreamingResponse


class FakeCodeBuddyServer:
    """Fake CodeBuddy daemon with canned responses."""
    
    def __init__(self):
        self.app = FastAPI()
        self.connections: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self._setup_routes()
    
    def _setup_routes(self):
        """Setup fake ACP endpoints."""
        
        @self.app.get("/api/v1/health")
        async def health():
            return {"status": "UP", "components": {"eg": {"status": "UP"}}}
        
        @self.app.post("/api/v1/acp/connect")
        async def acp_connect(x_codebuddy_request: str | None = Header(None)):
            if x_codebuddy_request != "1":
                raise HTTPException(status_code=400, detail="Missing x-codebuddy-request header")
            
            connection_id = f"conn_{uuid.uuid4().hex[:16]}"
            session_token = f"token_{uuid.uuid4().hex[:16]}"
            
            self.connections[connection_id] = {
                "id": connection_id,
                "token": session_token,
                "created_at": asyncio.get_event_loop().time(),
            }
            
            return {
                "connectionId": connection_id,
                "sessionToken": session_token,
            }
        
        @self.app.post("/api/v1/acp")
        async def acp_jsonrpc(
            request: dict[str, Any],
            x_codebuddy_request: str | None = Header(None),
            acp_connection_id: str | None = Header(None),
            accept: str | None = Header(None),
        ):
            """Handle JSON-RPC requests and return SSE stream."""
            if x_codebuddy_request != "1":
                raise HTTPException(status_code=400, detail="Missing x-codebuddy-request header")
            
            if not acp_connection_id or acp_connection_id not in self.connections:
                raise HTTPException(status_code=400, detail="Invalid connection ID")
            
            method = request.get("method")
            params = request.get("params", {})
            rpc_id = request.get("id")
            
            async def generate_sse():
                """Generate SSE stream for the request."""
                if method == "initialize":
                    # Return initialize result
                    result = {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {
                            "protocolVersion": 1,
                            "agentCapabilities": {},
                            "authMethods": [],
                        }
                    }
                    yield f"data: {json.dumps(result)}\n\n"
                
                elif method == "session/new":
                    # Create session and emit session/update events
                    session_id = f"sess_{uuid.uuid4().hex[:16]}"
                    self.sessions[session_id] = {"id": session_id}
                    
                    # Emit config_option_update notification
                    notification = {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "config_option_update",
                            }
                        }
                    }
                    yield f"data: {json.dumps(notification)}\n\n"
                    
                    # Emit result
                    result = {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {
                            "sessionId": session_id,
                            "models": ["default-model"],
                            "modes": ["code"],
                            "configOptions": {},
                        }
                    }
                    yield f"data: {json.dumps(result)}\n\n"
                
                elif method == "session/prompt":
                    # Emit agent_message_chunk events and result
                    session_id = params.get("sessionId")
                    
                    # Emit agent_message_chunk
                    chunk1 = {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "text": "Hello, ",
                            }
                        }
                    }
                    yield f"data: {json.dumps(chunk1)}\n\n"
                    
                    await asyncio.sleep(0.01)  # Simulate streaming delay
                    
                    chunk2 = {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "text": "world!",
                            }
                        }
                    }
                    yield f"data: {json.dumps(chunk2)}\n\n"
                    
                    # Emit usage_update
                    usage = {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "usage_update",
                                "used": 100,
                                "size": 1000,
                            }
                        }
                    }
                    yield f"data: {json.dumps(usage)}\n\n"
                    
                    # Emit result with stopReason
                    result = {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "result": {
                            "stopReason": "end_turn",
                            "userMessageId": "msg_123",
                            "usage": {
                                "inputTokens": 10,
                                "outputTokens": 15,
                                "totalTokens": 25,
                            }
                        }
                    }
                    yield f"data: {json.dumps(result)}\n\n"
                
                else:
                    # Unknown method
                    error = {
                        "jsonrpc": "2.0",
                        "id": rpc_id,
                        "error": {
                            "code": -32601,
                            "message": f"Method not found: {method}",
                        }
                    }
                    yield f"data: {json.dumps(error)}\n\n"
            
            return StreamingResponse(
                generate_sse(),
                media_type="text/event-stream",
            )
        
        @self.app.delete("/api/v1/acp")
        async def acp_disconnect(
            acp_connection_id: str | None = Header(None),
        ):
            """Close ACP connection."""
            if acp_connection_id and acp_connection_id in self.connections:
                del self.connections[acp_connection_id]
            return {"status": "ok"}


def create_fake_server() -> FastAPI:
    """Create a fake CodeBuddy server app."""
    server = FakeCodeBuddyServer()
    return server.app
