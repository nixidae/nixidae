# SPDX-License-Identifier: MIT
"""Pytest fixtures for grpclib-nri testing."""

import asyncio
import logging
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from grpclib_nri import NriServer

from .dummy_plugin import DummyPlugin


@pytest.fixture(scope="session")
def test_server_bin() -> Path:
    """Get the path to the nri-test-server binary from NRI_TEST_SERVER env var."""
    import os

    bin_path = os.environ.get("NRI_TEST_SERVER")
    if not bin_path:
        pytest.fail("NRI_TEST_SERVER environment variable not set")

    path = Path(bin_path)
    if not path.exists():
        pytest.fail(f"NRI_TEST_SERVER binary does not exist: {bin_path}")

    return path


@pytest.fixture
def socket_path(tmp_path: Path) -> Path:
    """Create a unique socket path for each test."""
    return tmp_path / "nri.sock"


@pytest_asyncio.fixture
async def test_server_process(
    test_server_bin: Path, socket_path: Path
) -> AsyncGenerator[asyncio.subprocess.Process, None]:
    """Start the Go NRI test server and yield the process.

    Waits for the server to be ready by checking if the socket exists.
    """
    logger = logging.getLogger("test.server_process")
    logger.info(f"Starting test server: {test_server_bin}")

    proc = await asyncio.create_subprocess_exec(
        str(test_server_bin),
        "-socket",
        str(socket_path),
        "-timeout",
        "5s",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    # Wait for socket to be created (max 5 seconds)
    max_retries = 50
    for _ in range(max_retries):
        if socket_path.exists():
            logger.info(f"Server ready: socket created at {socket_path}")
            break
        if proc.returncode is not None:
            stdout, stderr = await proc.communicate()
            logger.error(f"Server exited prematurely with code {proc.returncode}")
            if stdout:
                logger.error(f"stdout: {stdout.decode()}")
            if stderr:
                logger.error(f"stderr: {stderr.decode()}")
            pytest.skip("Test server failed to start")
        await asyncio.sleep(0.1)
    else:
        proc.terminate()
        await proc.wait()
        pytest.skip("Test server did not create socket in time")

    yield proc

    # Cleanup
    if proc.returncode is None:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            logger.warning("Test server did not terminate gracefully, killing...")
            proc.kill()
            await proc.wait()


@pytest_asyncio.fixture
async def nri_server(
    test_server_process: asyncio.subprocess.Process, socket_path: Path
) -> AsyncGenerator[NriServer, None]:
    """Create and start an NriServer connected to the test server.

    The test server is already running and listening on socket_path.
    We create a dummy plugin and start the NriServer to connect to it.
    """
    logger = logging.getLogger("test.nri_server")

    plugin = DummyPlugin()
    server = NriServer(
        plugin,
        socket_path,
        plugin_name="test-plugin",
        plugin_idx="42",
    )

    logger.info(f"Starting NriServer on {socket_path}")

    # Start server in background task
    server_task = asyncio.create_task(server.start())

    # Give the server time to connect and complete handshake
    await asyncio.sleep(2.0)

    try:
        yield server
    finally:
        logger.info("Closing NriServer")
        await server.close()

        # Give background task time to exit
        try:
            await asyncio.wait_for(server_task, timeout=2)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            logger.warning("Server task did not exit gracefully")


@pytest.fixture
def anyio_backend():
    """Configure pytest-asyncio to use asyncio backend."""
    return "asyncio"
