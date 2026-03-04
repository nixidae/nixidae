# SPDX-License-Identifier: MIT
"""Pytest configuration and fixtures for grpclib_ttrpc tests."""

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import AsyncGenerator

import pytest
import pytest_asyncio

# Ensure tests/ is on sys.path so helpers.py and dummy_pb2.py are importable.
_here = os.path.dirname(os.path.abspath(__file__))
if _here not in sys.path:
    sys.path.insert(0, _here)


@pytest.fixture(scope="session")
def test_server_bin() -> Path:
    """Get the path to the ttrpc-test-server binary from TTRPC_TEST_SERVER env var."""
    bin_path = os.environ.get("TTRPC_TEST_SERVER")
    if not bin_path:
        pytest.fail("TTRPC_TEST_SERVER environment variable not set")

    path = Path(bin_path)
    if not path.exists():
        pytest.fail(f"TTRPC_TEST_SERVER binary does not exist: {bin_path}")

    return path


@pytest.fixture
def socket_path(tmp_path: Path) -> Path:
    """Create a unique socket path for each test."""
    return tmp_path / "ttrpc.sock"


@pytest_asyncio.fixture
async def test_server_process(
    test_server_bin: Path, socket_path: Path
) -> AsyncGenerator[asyncio.subprocess.Process, None]:
    """Start the Go TTRPC test server and yield the process.

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
async def loop():
    return asyncio.get_running_loop()
