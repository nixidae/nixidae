# SPDX-License-Identifier: MIT
"""Integration tests for grpclib_ttrpc against Go test server.

Tests the Python client implementation against the real Go test server
(github.com/containerd/ttrpc/integration/streaming) to verify Python-Go
interoperability across all RPC cardinalities.

The Go server implements the Streaming service:
- Echo (unary)
- EchoStream (server streaming)
- SumStream (client streaming)
- DivideStream (bidirectional streaming)
"""

import asyncio
import logging
from pathlib import Path

import pytest
import pytest_asyncio
from grpclib.encoding.proto import ProtoCodec
from ttrpc.streaming_pb2 import EchoPayload, Part, Sum

from .helpers import FLAG_REMOTE_CLOSED, TtrpcClient

CODEC = ProtoCodec()


def enc(msg):
    return CODEC.encode(msg, type(msg))


def dec(payload: bytes, msg_type):
    return CODEC.decode(payload, msg_type)


@pytest_asyncio.fixture
async def streaming_client(test_server_process, socket_path: Path):
    """Create a raw TtrpcClient connected to the Go test server via Unix socket."""
    logger = logging.getLogger("test.streaming_client")
    logger.info(f"Connecting client to socket: {socket_path}")

    reader, writer = await asyncio.open_unix_connection(str(socket_path))
    client = TtrpcClient(reader, writer)
    yield client
    client.close()


# ---------------------------------------------------------------------------
# UNARY_UNARY
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unary_echo(streaming_client: TtrpcClient) -> None:
    """Test unary Echo RPC."""
    req = EchoPayload(seq=1, msg="hello")
    resp_bytes = await streaming_client.send_unary_request(
        "ttrpc.integration.streaming.Streaming",
        "Echo",
        enc(req),
    )
    from ttrpc.ttrpc_pb2 import Response

    resp = Response.FromString(resp_bytes)
    reply = dec(resp.payload, EchoPayload)
    assert reply.seq == 2  # Server increments seq
    assert reply.msg == "hello"


@pytest.mark.asyncio
async def test_unary_echo_multiple(streaming_client: TtrpcClient) -> None:
    """Test multiple unary Echo calls."""
    for seq in [1, 10, 100]:
        req = EchoPayload(seq=seq, msg=f"msg{seq}")
        resp_bytes = await streaming_client.send_unary_request(
            "ttrpc.integration.streaming.Streaming",
            "Echo",
            enc(req),
        )
        from ttrpc.ttrpc_pb2 import Response

        resp = Response.FromString(resp_bytes)
        reply = dec(resp.payload, EchoPayload)
        assert reply.seq == seq + 1


# ---------------------------------------------------------------------------
# UNARY_STREAM (server streaming)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_echo_stream(streaming_client: TtrpcClient) -> None:
    """Test server streaming EchoStream RPC."""
    req = EchoPayload(seq=42, msg="stream")
    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "EchoStream",
        enc(req),
    )
    # Close the stream from client side
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Server echoes back the payload
    flags, payload = await streaming_client.read_data_frame()
    reply = dec(payload, EchoPayload)
    assert reply.seq == 43
    assert reply.msg == "stream"


# ---------------------------------------------------------------------------
# STREAM_UNARY (client streaming)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sum_stream(streaming_client: TtrpcClient) -> None:
    """Test client streaming SumStream RPC (sums multiple Part messages)."""
    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "SumStream",
    )

    # Send multiple Part messages with numbers to sum
    values = [10, 20, 30]
    for value in values:
        part = Part(add=value)
        await streaming_client.send_data_frame(sid, enc(part))

    # Close stream
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Read response
    resp_bytes = await streaming_client._read_response()
    from ttrpc.ttrpc_pb2 import Response

    resp = Response.FromString(resp_bytes)
    result = dec(resp.payload, Sum)
    assert result.sum == sum(values)  # 10 + 20 + 30 = 60


# ---------------------------------------------------------------------------
# STREAM_STREAM (bidirectional streaming)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_divide_stream(streaming_client: TtrpcClient) -> None:
    """Test bidirectional streaming DivideStream RPC.

    Sends a Sum message and receives streamed Part responses.
    """
    sid = await streaming_client.open_request(
        "ttrpc.integration.streaming.Streaming",
        "DivideStream",
        enc(Sum(sum=100, num=5)),  # Server will divide 100/5 = 20, 5 times
    )

    # Close stream from client side
    await streaming_client.send_data_frame(sid, b"", close=True)

    # Collect all Part responses
    parts = []
    while True:
        flags, payload = await streaming_client.read_data_frame()
        if payload:
            parts.append(dec(payload, Part))
        if flags & FLAG_REMOTE_CLOSED:
            break

    # Server returns num=5 Part messages with avg=20
    assert len(parts) == 5
    for part in parts:
        assert part.add == 20  # 100 / 5 = 20


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_server_starts(test_server_process, socket_path: Path) -> None:
    """Smoke test: Verify Go test server starts and creates socket."""
    logger = logging.getLogger("test.server_starts")

    assert socket_path.exists(), f"Server socket not created at {socket_path}"
    assert test_server_process.returncode is None, (
        f"Server exited with code {test_server_process.returncode}"
    )

    logger.info("Go test server is running and socket is accessible")
