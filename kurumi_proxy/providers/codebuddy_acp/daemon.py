"""
CodeBuddy ACP daemon lifecycle manager.

Spawns `codebuddy --acp --acp-transport streamable-http -y`, parses stdout
to discover the auto-assigned port, performs the /connect handshake, and
calls initialize to verify protocol version.
"""

import asyncio
import logging
import re
import time
from typing import Optional

import httpx

from kurumi_proxy.providers.codebuddy_acp.client import AcpJsonRpcClient

logger = logging.getLogger(__name__)

# Regex to parse the daemon's stdout endpoint announcement
ENDPOINT_REGEX = re.compile(r"ACP streamable-http endpoint: (http://127\.0\.0\.1:\d+/api/v1/acp)")


class AcpDaemonStartupError(Exception):
    """Raised when daemon fails to start or endpoint discovery times out."""
    pass


class AcpDaemon:
    """
    Manages a persistent CodeBuddy ACP daemon process.
    
    Lifecycle:
    1. start() - spawn process, discover port, connect, initialize
    2. is_running - check if daemon is alive
    3. stop() - graceful shutdown (SIGTERM, wait, SIGKILL)
    4. restart() - stop then start
    """
    
    def __init__(
        self,
        *,
        codebuddy_bin: str = "codebuddy",
        startup_timeout: float = 10.0,
    ):
        self.codebuddy_bin = codebuddy_bin
        self.startup_timeout = startup_timeout
        
        # Populated after successful start()
        self.base_url: Optional[str] = None
        self.connection_id: Optional[str] = None
        self.session_token: Optional[str] = None
        self.rpc_client: Optional[AcpJsonRpcClient] = None
        
        # Internal state
        self._process: Optional[asyncio.subprocess.Process] = None
        self._lock = asyncio.Lock()
        self._last_health_check: Optional[float] = None
    
    @property
    def is_running(self) -> bool:
        """Check if daemon process is alive and connected."""
        if self._process is None:
            return False
        if self._process.returncode is not None:
            # Process has exited
            return False
        return self.rpc_client is not None
    
    async def start(self) -> None:
        """
        Start the daemon and perform the connection handshake.
        
        Raises AcpDaemonStartupError on failure.
        """
        async with self._lock:
            if self.is_running:
                logger.debug("Daemon already running")
                return
            
            await self._start_locked()
    
    async def _start_locked(self) -> None:
        """Internal start (must hold _lock)."""
        logger.info(f"Starting CodeBuddy ACP daemon: {self.codebuddy_bin}")
        
        try:
            # Spawn: codebuddy --acp --acp-transport streamable-http -y
            #
            # NOTE: The daemon emits its endpoint announcement
            # ("ACP streamable-http endpoint: http://127.0.0.1:<PORT>/api/v1/acp")
            # to STDERR, not stdout. We merge stderr into stdout so we can
            # use a single reader to discover the endpoint.
            self._process = await asyncio.create_subprocess_exec(
                self.codebuddy_bin,
                "--acp",
                "--acp-transport", "streamable-http",
                "-y",  # dangerously-skip-permissions
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            
            pid = self._process.pid
            logger.info(f"Daemon spawned with PID {pid}")
            
            # Read stdout line-by-line to find the endpoint URL
            self.base_url = await self._discover_endpoint()
            logger.info(f"Daemon endpoint discovered: {self.base_url}")
            
            # Perform /connect handshake
            await self._connect()
            logger.info(f"ACP connected: connectionId={self.connection_id}")
            
            # Create RPC client
            self.rpc_client = AcpJsonRpcClient(
                base_url=self.base_url,
                connection_id=self.connection_id,
                session_token=self.session_token,
            )
            
            # Call initialize to verify protocol version
            await self._initialize()
            logger.info("ACP protocol initialized (protocolVersion=1)")
            
            self._last_health_check = time.time()
            
        except Exception as exc:
            logger.error(f"Daemon startup failed: {exc}")
            await self._kill_process()
            raise AcpDaemonStartupError(str(exc)) from exc
    
    async def _discover_endpoint(self) -> str:
        """
        Read daemon stdout until we see the endpoint announcement line.
        
        Returns the base URL (e.g., "http://127.0.0.1:54321/api/v1/acp").
        Raises AcpDaemonStartupError on timeout or process exit.
        """
        assert self._process is not None
        assert self._process.stdout is not None
        
        deadline = time.monotonic() + self.startup_timeout
        captured_output = []
        
        while time.monotonic() < deadline:
            # Check if process exited
            if self._process.returncode is not None:
                # stderr was merged into stdout, so any error output is
                # already in captured_output.
                raise AcpDaemonStartupError(
                    f"Daemon exited with code {self._process.returncode}. "
                    f"output: {''.join(captured_output)}"
                )
            
            # Try to read a line with timeout
            try:
                remaining = deadline - time.monotonic()
                line_bytes = await asyncio.wait_for(
                    self._process.stdout.readline(),
                    timeout=min(remaining, 1.0),
                )
            except asyncio.TimeoutError:
                continue
            
            if not line_bytes:
                # EOF
                break
            
            line = line_bytes.decode(errors="replace").strip()
            captured_output.append(line)
            logger.debug(f"Daemon stdout: {line}")
            
            # Try to match the endpoint pattern
            match = ENDPOINT_REGEX.search(line)
            if match:
                return match.group(1)
        
        raise AcpDaemonStartupError(
            f"Endpoint discovery timeout after {self.startup_timeout}s. "
            f"Captured output: {''.join(captured_output)}"
        )
    
    async def _connect(self) -> None:
        """
        POST /api/v1/acp/connect to get connectionId and sessionToken.
        """
        assert self.base_url is not None
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                f"{self.base_url}/connect",
                json={},
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            
            data = response.json()
            self.connection_id = data["connectionId"]
            self.session_token = data.get("sessionToken")
    
    async def _initialize(self) -> None:
        """
        Call JSON-RPC `initialize` and verify protocolVersion=1.
        """
        assert self.rpc_client is not None
        
        params = {
            "protocolVersion": 1,
            "clientCapabilities": {
                "fs": {"readTextFile": False, "writeTextFile": False}
            }
        }
        
        result = None
        async for event in self.rpc_client.call("initialize", params):
            if "result" in event:
                result = event["result"]
                break
            elif "error" in event:
                error = event["error"]
                raise AcpDaemonStartupError(f"initialize error: {error}")
        
        if result is None:
            raise AcpDaemonStartupError("initialize did not return result")
        
        protocol_version = result.get("protocolVersion")
        if protocol_version != 1:
            raise AcpDaemonStartupError(
                f"Unsupported protocol version: {protocol_version} (expected 1)"
            )
    
    async def stop(self) -> None:
        """
        Gracefully stop the daemon (SIGTERM, wait 5s, SIGKILL).
        """
        async with self._lock:
            await self._stop_locked()
    
    async def _stop_locked(self) -> None:
        """Internal stop (must hold _lock)."""
        if self._process is None:
            return
        
        logger.info(f"Stopping daemon PID {self._process.pid}")
        
        # Close RPC client if present
        if self.rpc_client is not None:
            await self.rpc_client.close()
            self.rpc_client = None
        
        # Graceful shutdown
        await self._kill_process()
        
        self.base_url = None
        self.connection_id = None
        self.session_token = None
    
    async def _kill_process(self) -> None:
        """Terminate the daemon process (SIGTERM, wait, SIGKILL)."""
        if self._process is None:
            return
        
        try:
            self._process.terminate()
            await asyncio.wait_for(self._process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Daemon did not terminate gracefully, killing")
            self._process.kill()
            await self._process.wait()
        except ProcessLookupError:
            # Process already exited
            pass
        
        self._process = None
    
    async def restart(self) -> None:
        """Stop then start the daemon."""
        async with self._lock:
            await self._stop_locked()
            await self._start_locked()
    
    async def health_check(self) -> bool:
        """
        Probe daemon health by re-issuing a cheap `initialize` RPC.
        
        Returns True if healthy, False otherwise. Updates _last_health_check
        on success.
        
        Note: There is no /api/health endpoint on the real daemon. This
        method uses initialize as a lightweight probe.
        """
        if not self.is_running:
            return False
        
        try:
            async for event in self.rpc_client.call("initialize", {"protocolVersion": 1}):
                if "result" in event:
                    self._last_health_check = time.time()
                    return True
                elif "error" in event:
                    logger.warning(f"Health check failed: {event['error']}")
                    return False
        except Exception as exc:
            logger.warning(f"Health check exception: {exc}")
            return False
        
        return False
    
    def get_status(self) -> dict:
        """Get current daemon status for monitoring."""
        return {
            "is_running": self.is_running,
            "base_url": self.base_url,
            "connection_id": self.connection_id,
            "last_health_check": self._last_health_check,
            "pid": self._process.pid if self._process else None,
        }
