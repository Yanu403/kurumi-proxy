"""
Unit tests for ACP daemon lifecycle management.

Tests daemon spawning, endpoint discovery, connect handshake, and shutdown.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from kurumi_proxy.providers.codebuddy_acp.daemon import (
    AcpDaemon,
    AcpDaemonStartupError,
    ENDPOINT_REGEX,
)


def test_endpoint_regex():
    """Test the endpoint discovery regex matches expected format."""
    line = "ACP streamable-http endpoint: http://127.0.0.1:54321/api/v1/acp"
    match = ENDPOINT_REGEX.search(line)
    assert match is not None
    assert match.group(1) == "http://127.0.0.1:54321/api/v1/acp"
    
    # Should not match other lines
    line2 = "Some other output"
    assert ENDPOINT_REGEX.search(line2) is None


@pytest.fixture
def daemon():
    """Create daemon instance for testing."""
    return AcpDaemon(
        codebuddy_bin="codebuddy",
        startup_timeout=5.0,
    )


@pytest.fixture
def mock_process():
    """Mock subprocess for testing."""
    process = MagicMock()
    process.pid = 12345
    process.returncode = None
    process.terminate = MagicMock()
    process.kill = MagicMock()
    process.wait = AsyncMock(return_value=0)
    
    # Mock stdout to return endpoint line
    stdout = MagicMock()
    stdout.readline = AsyncMock(
        return_value=b"ACP streamable-http endpoint: http://127.0.0.1:54321/api/v1/acp\n"
    )
    process.stdout = stdout
    
    # Mock stderr
    stderr = MagicMock()
    stderr.read = AsyncMock(return_value=b"")
    process.stderr = stderr
    
    return process


@pytest.mark.asyncio
async def test_daemon_discover_endpoint_success(daemon, mock_process):
    """Test successful endpoint discovery from stdout."""
    daemon._process = mock_process
    
    base_url = await daemon._discover_endpoint()
    
    assert base_url == "http://127.0.0.1:54321/api/v1/acp"


@pytest.mark.asyncio
async def test_daemon_discover_endpoint_timeout(daemon):
    """Test endpoint discovery times out."""
    process = MagicMock()
    process.returncode = None
    stdout = MagicMock()
    # Simulate slow output that never matches
    stdout.readline = AsyncMock(return_value=b"Some other output\n")
    process.stdout = stdout
    daemon._process = process
    daemon.startup_timeout = 0.1
    
    with pytest.raises(AcpDaemonStartupError, match="timeout"):
        await daemon._discover_endpoint()


@pytest.mark.asyncio
async def test_daemon_discover_endpoint_process_exited(daemon):
    """Test endpoint discovery when process exits early."""
    process = MagicMock()
    process.returncode = 1
    stdout = MagicMock()
    stdout.readline = AsyncMock(return_value=b"")
    process.stdout = stdout
    stderr = MagicMock()
    stderr.read = AsyncMock(return_value=b"Error: binary not found")
    process.stderr = stderr
    daemon._process = process
    
    with pytest.raises(AcpDaemonStartupError, match="exited"):
        await daemon._discover_endpoint()


@pytest.mark.asyncio
async def test_daemon_connect(daemon):
    """Test /connect handshake."""
    daemon.base_url = "http://127.0.0.1:54321/api/v1/acp"
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "connectionId": "conn_123",
        "sessionToken": "token_456",
    }
    mock_response.raise_for_status = MagicMock()
    
    mock_client = MagicMock()
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    
    with patch("httpx.AsyncClient", return_value=mock_client):
        await daemon._connect()
    
    assert daemon.connection_id == "conn_123"
    assert daemon.session_token == "token_456"


@pytest.mark.asyncio
async def test_daemon_stop_graceful(daemon, mock_process):
    """Test graceful shutdown."""
    daemon._process = mock_process
    
    await daemon._kill_process()
    
    mock_process.terminate.assert_called_once()
    mock_process.wait.assert_called_once()
    assert daemon._process is None


@pytest.mark.asyncio
async def test_daemon_stop_force_kill(daemon):
    """Test force kill when graceful termination times out."""
    process = MagicMock()
    process.terminate = MagicMock()
    process.kill = MagicMock()
    # First wait times out, second succeeds
    process.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), 0])
    daemon._process = process
    
    await daemon._kill_process()
    
    process.terminate.assert_called_once()
    process.kill.assert_called_once()
    assert daemon._process is None


def test_daemon_is_running_false_initially(daemon):
    """Test is_running is False before start."""
    assert daemon.is_running is False


def test_daemon_get_status(daemon):
    """Test status reporting."""
    status = daemon.get_status()
    
    assert status["is_running"] is False
    assert status["base_url"] is None
    assert status["connection_id"] is None
    assert status["pid"] is None
