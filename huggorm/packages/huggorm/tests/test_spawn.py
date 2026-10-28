"""A server this process starts and stops (nixidae/nixidae#1060)."""

import os

import anyio
import pytest

from huggorm import ServerOptions, remote
from huggorm.spawn import ServerExited, spawned
from huggorm_generated import AsyncEvalState, AsyncStore


async def test_the_socket_answers_once_yielded() -> None:
    async with spawned() as path, remote.connect(path) as client:
        store = await client.acquire(AsyncStore, "dummy://")
        state = await client.acquire(AsyncEvalState, store)
        assert await (await state.eval_expr("1 + 1")).integer() == 2
    assert not await anyio.Path(os.path.dirname(path)).exists()


async def test_a_quiet_server_writes_nothing_to_stderr(capfd: pytest.CaptureFixture[str]) -> None:
    """The server's stderr is the caller's: a CLI's user reads every
    line, so a line there must be news. `-m huggorm.server` printed a
    runpy RuntimeWarning on every start."""
    async with spawned() as path, remote.connect(path) as client:
        await client.acquire(AsyncStore, "dummy://")
    assert capfd.readouterr().err == ""


async def test_a_server_that_dies_first_is_reported() -> None:
    """Its exit status, and not a timeout: the pipe closes with it."""
    with anyio.fail_after(30), pytest.raises(ServerExited, match="status"):
        async with spawned(ServerOptions(settings={"no-such-setting": "1"})):
            pass
