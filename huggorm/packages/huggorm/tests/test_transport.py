"""A server on a stream other than its Unix socket (nixidae/nixidae#551).

`serve_stream` serves one connection on any byte stream, and a client
dials it through `huggorm.transport`. `dummy://` is in-memory, so the
tests here are hermetic.
"""

import os
from collections.abc import AsyncIterator

import anyio
import pytest

from huggorm import transport
from huggorm.protocol import ConnectionLost
from huggorm.remote import NixClient
from huggorm.server import serve_stream
from huggorm.session import AsyncRemoteSession


@pytest.fixture
async def pipe_server() -> AsyncIterator[transport.Dialer]:
    """A server in this process on two pipes, and a dialer for its far
    end. Leaving the fixture closes the client's end, and the server
    returns at EOF."""
    to_server_read, to_server_write = os.pipe()
    to_client_read, to_client_write = os.pipe()
    client_end = transport.FdStream(to_client_read, to_server_write)
    async with anyio.create_task_group() as tg:
        await tg.start(serve_stream, transport.FdStream(to_server_read, to_client_write))
        yield transport.once(client_end, "the test pipes")
        with anyio.fail_after(10):
            await client_end.aclose()


async def test_a_server_on_pipes_evaluates(pipe_server: transport.Dialer) -> None:
    async with AsyncRemoteSession.connect(pipe_server, store_uri="dummy://") as session:
        state = await session.eval(await session.store())
        value = await state.eval_expr("1 + 1")
        assert await value.integer() == 2


async def test_a_pipe_refuses_a_second_connection(pipe_server: transport.Dialer) -> None:
    async with NixClient(pipe_server) as client:
        await client.bind()
        with pytest.raises(ConnectionLost, match="carries one connection"):
            await client.bind()
