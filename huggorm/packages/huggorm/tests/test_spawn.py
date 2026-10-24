"""A server this process starts and stops (nixidae/nixidae#1060)."""

import os

import anyio
import pytest

from huggorm import remote
from huggorm.spawn import ServerExited, spawned


async def test_the_socket_answers_once_yielded() -> None:
    async with spawned() as path, remote.connect(path) as client:
        store = await client.acquire("Store", "dummy://")
        state = await client.acquire("EvalState", store)
        assert await (await state.eval_expr("1 + 1")).integer() == 2
    assert not await anyio.Path(os.path.dirname(path)).exists()


async def test_a_server_that_dies_first_is_reported() -> None:
    """Its exit status, and not a timeout: the pipe closes with it."""
    with anyio.fail_after(30), pytest.raises(ServerExited, match="status"):
        async with spawned("--option", "no-such-setting", "1"):
            pass
