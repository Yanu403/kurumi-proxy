"""
CodeBuddy daemon lifecycle manager.

Manages a persistent `codebuddy --serve` process with health checks,
auto-restart, and graceful shutdown.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

logger = logging.getLogger(__name__)


@dataclass
class DaemonState:
    """Current state of the CodeBuddy daemon."""
    pid: Optional[int] = None
    port: int = 6275
    boot_count: int = 0
    last_health_ok: Optional[float] = None
    last_error: Optional[str] = None
    process: Optional[asyncio.subprocess.Process] = None


class AcpDaemon:
    """
    Manages the CodeBuddy CLI daemon lifecycle.
    
    Features:
    - Automatic startup with health checks
    - Auto-restart on failure
    - Graceful shutdown
    - State tracking for monitoring
    """
    
    def __init__(
        self,
        *,
        port: int = 6275,
        host: str = "127.0.0.1",
        codebuddy_bin: str = "codebuddy",
        startup_timeout: float = 10.0,
        health_check_timeout: float = 2.0,
    ):
        self.port = port
        self.host = host
        self.codebuddy_bin = codebuddy_bin
        self.startup_timeout = startup_timeout
        self.health_check_timeout = health_check_timeout
        self.state = DaemonState(port=port)
        self._lock = asyncio.Lock()
    
    @property
    def base_url(self) -> str:
        """Base URL for daemon HTTP API."""
        return f"http://{self.host}:{self.port}"
    
    async def ensure_running(self) -> None:
        """
        Ensure daemon is running and healthy.
        
        Starts daemon if not running, or restarts if unhealthy.
        """
        async with self._lock:
            if await self._is_healthy():
                return
            
            # Stop any existing unhealthy process
            if self.state.process is not None:
                await self._stop_process()
            
            # Start new process
            await self._start_process()
    
    async def _start_process(self) -> None:
        """Start the CodeBuddy daemon process."""
        logger.info(f"Starting CodeBuddy daemon on {self.host}:{self.port}")
        
        try:
            # Launch: codebuddy --serve --port <port> --host <host> -y
            # -y skips permission gates to avoid deadlock
            self.state.process = await asyncio.create_subprocess_exec(
                self.codebuddy_bin,
                "--serve",
                "--port", str(self.port),
                "--host", self.host,
                "-y",  # dangerously-skip-permissions
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            
            self.state.pid = self.state.process.pid
            self.state.boot_count += 1
            logger.info(f"Daemon started with PID {self.state.pid} (boot #{self.state.boot_count})")
            
            # Wait for health check to pass
            deadline = time.monotonic() + self.startup_timeout
            while time.monotonic() < deadline:
                if await self._check_health():
                    logger.info("Daemon health check passed")
                    return
                await asyncio.sleep(0.5)
            
            # Health check timeout
            self.state.last_error = "Daemon health check timeout"
            logger.error(self.state.last_error)
            await self._stop_process()
            raise RuntimeError(self.state.last_error)
            
        except FileNotFoundError as exc:
            self.state.last_error = f"CodeBuddy binary not found: {self.codebuddy_bin}"
            logger.error(self.state.last_error)
            raise RuntimeError(self.state.last_error) from exc
        except Exception as exc:
            self.state.last_error = f"Failed to start daemon: {exc}"
            logger.error(self.state.last_error)
            raise RuntimeError(self.state.last_error) from exc
    
    async def _stop_process(self) -> None:
        """Stop the daemon process gracefully."""
        if self.state.process is None:
            return
        
        logger.info(f"Stopping daemon PID {self.state.pid}")
        
        try:
            self.state.process.terminate()
            await asyncio.wait_for(self.state.process.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Daemon did not terminate gracefully, killing")
            self.state.process.kill()
            await self.state.process.wait()
        
        self.state.process = None
        self.state.pid = None
    
    async def _check_health(self) -> bool:
        """
        Check daemon health via GET /api/v1/health.
        
        Returns True if healthy, False otherwise.
        """
        try:
            async with httpx.AsyncClient(timeout=self.health_check_timeout) as client:
                response = await client.get(f"{self.base_url}/api/v1/health")
                if response.status_code == 200:
                    data = response.json()
                    if data.get("status") == "UP":
                        self.state.last_health_ok = time.time()
                        self.state.last_error = None
                        return True
        except Exception as exc:
            logger.debug(f"Health check failed: {exc}")
        
        return False
    
    async def _is_healthy(self) -> bool:
        """Check if daemon is running and healthy."""
        if self.state.process is None:
            return False
        
        # Check if process is still alive
        if self.state.process.returncode is not None:
            logger.warning(f"Daemon process exited with code {self.state.process.returncode}")
            self.state.process = None
            self.state.pid = None
            return False
        
        return await self._check_health()
    
    async def shutdown(self) -> None:
        """Gracefully shutdown the daemon."""
        async with self._lock:
            await self._stop_process()
    
    def get_status(self) -> dict:
        """Get current daemon status for monitoring."""
        return {
            "pid": self.state.pid,
            "port": self.port,
            "boot_count": self.state.boot_count,
            "last_health_ok": self.state.last_health_ok,
            "last_error": self.state.last_error,
            "is_running": self.state.process is not None,
        }
