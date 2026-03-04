# SPDX-License-Identifier: MIT
"""Integration test infrastructure for grpclib_ttrpc with Go test server.

This module sets up fixtures for testing against the real Go test server
(github.com/containerd/ttrpc/integration/streaming) to verify Python-Go
interoperability.

The Go test server implements the Streaming service with these methods:
- Echo (unary)
- EchoStream (server streaming)
- SumStream (client streaming)
- DivideStream (bidirectional streaming)
- EchoNull (client streaming returning empty)
- EchoNullStream (bidirectional streaming returning empty)
- EmptyPayloadStream (server streaming from empty input)

Full integration tests require generating Python protobuf code from
ttrpc/integration/streaming/test.proto. For now, this module documents
the test server infrastructure and fixtures.
"""

import logging
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_server_starts(test_server_process, socket_path: Path) -> None:
    """Smoke test: Verify Go test server starts and creates socket."""
    logger = logging.getLogger("test.server_starts")

    # If we got here without timeout, the server started successfully
    assert socket_path.exists(), f"Server socket not created at {socket_path}"

    # Verify the server is still running
    assert test_server_process.returncode is None, (
        f"Server exited with code {test_server_process.returncode}"
    )

    logger.info("Go test server is running and socket is accessible")


@pytest.mark.asyncio
async def test_socket_is_writable(test_server_process, socket_path: Path) -> None:
    """Verify the server socket is writable/accessible."""
    logger = logging.getLogger("test.socket_writable")

    import asyncio

    try:
        # Try to open a connection to the socket
        reader, writer = await asyncio.open_unix_connection(str(socket_path))
        logger.info(f"Successfully connected to server socket at {socket_path}")
        writer.close()
    except Exception as e:
        pytest.fail(f"Failed to connect to server socket: {e}")
