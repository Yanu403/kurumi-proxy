"""
Integration tests for ACP daemon lifecycle management.

Tests daemon startup, health checks, restart, and graceful shutdown.
"""

import pytest
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from kurumi_proxy.providers.codebuddy_acp.daemon import AcpDaemon, DaemonState


@pytest.fixture
def mock_process():
    """Mock subprocess for testing."""
    process = MagicMock()
    process.pid = 12345
    process.returncode = None
    process.terminate = MagicMock()
    process.kill = MagicMock()
    process.wait = AsyncMock(return_value=0)
    return process


@pytest.fixture
def daemon():
    """Create daemon instance for testing."""
    return AcpDaemon(
        port=6275,
        host="127.0.0.1",
        codebuddy_bin="codebuddy",
        startup_timeout=5.0,
        health_check_timeout=1.0,
    )


@pytest.mark.asyncio
async def test_daemon_startup_success(daemon, mock_process):
    """Test successful daemon startup with health check."""
    
    with patch('asyncio.create_subprocess_exec', return_value=mock_process):
        with patch.object(daemon, '_check_health', return_value=True):
            await daemon._start_process()
    
    assert daemon.state.pid == 12345
    assert daemon.state.boot_count == 1
    assert daemon.state.process == mock_process
    assert daemon.state.last_error is None


@pytest.mark.asyncio
async def test_daemon_startup_health_check_timeout(daemon, mock_process):
    """Test daemon startup fails when health check times out."""
    
    with patch('asyncio.create_subprocess_exec', return_value=mock_process):
        with patch.object(daemon, '_check_health', return_value=False):
            with pytest.raises(RuntimeError, match="health check timeout"):
                await daemon._start_process()
    
    assert daemon.state.last_error is not None
    assert "timeout" in daemon.state.last_error


@pytest.mark.asyncio
async def test_daemon_startup_binary_not_found(daemon):
    """Test daemon startup fails when binary not found."""
    
    with patch('asyncio.create_subprocess_exec', side_effect=FileNotFoundError()):
        with pytest.raises(RuntimeError, match="binary not found"):
            await daemon._start_process()
    
    assert daemon.state.last_error is not None
    assert "not found" in daemon.state.last_error


@pytest.mark.asyncio
async def test_daemon_ensure_running_starts_if_not_running(daemon, mock_process):
    """Test ensure_running starts daemon if not already running."""
    
    with patch('asyncio.create_subprocess_exec', return_value=mock_process):
        with patch.object(daemon, '_check_health', return_value=True):
            await daemon.ensure_running()
    
    assert daemon.state.pid == 12345
    assert daemon.state.boot_count == 1


@pytest.mark.asyncio
async def test_daemon_ensure_running_no_op_if_healthy(daemon, mock_process):
    """Test ensure_running does nothing if daemon already healthy."""
    
    # Start daemon first
    with patch('asyncio.create_subprocess_exec', return_value=mock_process):
        with patch.object(daemon, '_check_health', return_value=True):
            await daemon.ensure_running()
    
    boot_count_before = daemon.state.boot_count
    
    # Call ensure_running again - should be no-op
    with patch.object(daemon, '_check_health', return_value=True):
        await daemon.ensure_running()
    
    assert daemon.state.boot_count == boot_count_before  # No restart


@pytest.mark.asyncio
async def test_daemon_ensure_running_restarts_if_unhealthy(daemon, mock_process):
    """Test ensure_running restarts daemon if unhealthy."""
    
    # Start daemon first
    with patch('asyncio.create_subprocess_exec', return_value=mock_process):
        with patch.object(daemon, '_check_health', return_value=True):
            await daemon.ensure_running()
    
    boot_count_before = daemon.state.boot_count
    
    # Simulate daemon becoming unhealthy
    mock_process.returncode = 1  # Process exited
    
    # Create new mock for restarted process
    new_mock_process = MagicMock()
    new_mock_process.pid = 54321
    new_mock_process.returncode = None
    new_mock_process.wait = AsyncMock(return_value=0)
    
    # Call ensure_running - should restart
    with patch('asyncio.create_subprocess_exec', return_value=new_mock_process):
        with patch.object(daemon, '_check_health', return_value=True):
            await daemon.ensure_running()
    
    assert daemon.state.boot_count == boot_count_before + 1  # Restarted
    assert daemon.state.pid == 54321


@pytest.mark.asyncio
async def test_daemon_stop_process_graceful(daemon, mock_process):
    """Test graceful process termination."""
    
    daemon.state.process = mock_process
    daemon.state.pid = 12345
    
    await daemon._stop_process()
    
    mock_process.terminate.assert_called_once()
    mock_process.wait.assert_called_once()
    assert daemon.state.process is None
    assert daemon.state.pid is None


@pytest.mark.asyncio
async def test_daemon_stop_process_force_kill(daemon, mock_process):
    """Test force kill when graceful termination times out."""
    
    daemon.state.process = mock_process
    daemon.state.pid = 12345
    
    # Make wait timeout on first call, succeed on second
    mock_process.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), 0])
    
    await daemon._stop_process()
    
    mock_process.terminate.assert_called_once()
    mock_process.kill.assert_called_once()
    assert daemon.state.process is None


@pytest.mark.asyncio
async def test_daemon_shutdown(daemon, mock_process):
    """Test daemon shutdown."""
    
    # Start daemon
    with patch('asyncio.create_subprocess_exec', return_value=mock_process):
        with patch.object(daemon, '_check_health', return_value=True):
            await daemon.ensure_running()
    
    # Shutdown
    await daemon.shutdown()
    
    mock_process.terminate.assert_called_once()
    assert daemon.state.process is None
    assert daemon.state.pid is None


def test_daemon_get_status(daemon):
    """Test daemon status reporting."""
    
    daemon.state.pid = 12345
    daemon.state.boot_count = 3
    daemon.state.last_health_ok = 1234567890.0
    daemon.state.last_error = None
    daemon.state.process = MagicMock()
    
    status = daemon.get_status()
    
    assert status["pid"] == 12345
    assert status["port"] == 6275
    assert status["boot_count"] == 3
    assert status["last_health_ok"] == 1234567890.0
    assert status["last_error"] is None
    assert status["is_running"] is True


def test_daemon_get_status_not_running(daemon):
    """Test daemon status when not running."""
    
    status = daemon.get_status()
    
    assert status["pid"] is None
    assert status["is_running"] is False


def test_daemon_base_url(daemon):
    """Test base URL construction."""
    
    assert daemon.base_url == "http://127.0.0.1:6275"


@pytest.mark.asyncio
async def test_daemon_check_health_success(daemon):
    """Test health check with successful response."""
    
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {"status": "UP"}
    
    mock_client = MagicMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    
    with patch('httpx.AsyncClient', return_value=mock_client):
        result = await daemon._check_health()
    
    assert result is True
    assert daemon.state.last_health_ok is not None
    assert daemon.state.last_error is None


@pytest.mark.asyncio
async def test_daemon_check_health_failure(daemon):
    """Test health check with failed response."""
    
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=Exception("Connection failed"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock()
    
    with patch('httpx.AsyncClient', return_value=mock_client):
        result = await daemon._check_health()
    
    assert result is False
