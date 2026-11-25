"""arpyc over a socketpair: a call, and how the channel fails."""

from __future__ import annotations

import contextlib
import socket
import struct
from collections.abc import AsyncIterator, Awaitable, Callable

import anyio
import pytest
from vivarium_agent import arpyc
from vivarium_agent.arpyc import AsyncConnection, ProtocolError, Service


class Echo(Service):
    def __init__(self) -> None:
        self.cancelled = anyio.Event()
        self.started = anyio.Event()

    def exposed_echo(self, value: object) -> object:
        return value

    def exposed_late(self) -> None:
        raise TimeoutError("ran past its 1s timeout")

    def exposed_several(self) -> None:
        raise ExceptionGroup("several failed", [ValueError("the first")])

    async def exposed_hang(self) -> None:
        self.started.set()
        try:
            await anyio.sleep_forever()
        finally:
            self.cancelled.set()


def run(main: Callable[[], Awaitable[None]]) -> None:
    anyio.run(main)


@contextlib.asynccontextmanager
async def pair(service: Service) -> AsyncIterator[tuple[AsyncConnection, AsyncConnection]]:
    """A client and a serving peer, each on one end of a socketpair, both served until the block ends."""
    ours, theirs = socket.socketpair()
    peer = arpyc.listen(theirs.detach(), service, raw_tty=False)
    conn = arpyc.connect(ours.detach())
    async with anyio.create_task_group() as group:
        await group.start(peer.serve_forever)
        await group.start(conn.serve_forever)
        yield conn, peer
        conn.close()
        peer.close()


@contextlib.asynccontextmanager
async def raw() -> AsyncIterator[tuple[AsyncConnection, socket.socket]]:
    """A served client, and the other end as a plain socket the test writes by hand."""
    ours, theirs = socket.socketpair()
    conn = arpyc.connect(ours.detach())
    async with anyio.create_task_group() as group:
        await group.start(conn.serve_forever)
        yield conn, theirs
        conn.close()
        theirs.close()


def test_a_call_returns_the_peers_answer() -> None:
    async def main() -> None:
        async with pair(Echo()) as (conn, _):
            assert await conn.call("echo", ({"a": [1, 2]},), {}) == {"a": (1, 2)}

    run(main)


def test_a_timeout_in_the_peer_arrives_as_a_timeout() -> None:
    """`Machine._ask` tells the guest's own timeout from silence by its type."""

    async def main() -> None:
        async with pair(Echo()) as (conn, _):
            with pytest.raises(TimeoutError) as raised:
                await conn.call("late", (), {})
            assert raised.value.args == ("ran past its 1s timeout",)

    run(main)


def test_an_exception_group_arrives_and_the_channel_stays_up() -> None:
    """vinegar cannot rebuild a group; it arrives as a generic error with
    its text, and the next call is answered."""

    async def main() -> None:
        # Unread, the reply leaves the call waiting forever.
        with anyio.fail_after(10):
            await calls()

    async def calls() -> None:
        async with pair(Echo()) as (conn, _):
            with pytest.raises(Exception, match=r"ExceptionGroup.*several failed") as caught:
                await conn.call("several", (), {})
            assert not isinstance(caught.value, BaseExceptionGroup)
            assert await conn.call("echo", ("still here",), {}) == "still here"

    run(main)


def test_a_call_after_close_raises_at_once() -> None:
    async def main() -> None:
        async with pair(Echo()) as (conn, _):
            conn.close()
            with anyio.fail_after(1), pytest.raises(EOFError):
                await conn.call("echo", (1,), {})

    run(main)


def test_a_call_with_nothing_serving_raises_at_once() -> None:
    """Its reply would never be read: the call fails, it does not hang."""

    async def main() -> None:
        ours, theirs = socket.socketpair()
        conn = arpyc.connect(ours.detach())
        with anyio.fail_after(1), pytest.raises(RuntimeError, match="nothing serves"):
            await conn.call("echo", (1,), {})
        conn.close()
        theirs.close()

    run(main)


def test_a_peer_that_stops_reading_stalls_the_call_not_the_loop() -> None:
    async def main() -> None:
        async with raw() as (conn, _):
            ticks = 0

            async def tick() -> None:
                nonlocal ticks
                while True:
                    ticks += 1
                    await anyio.sleep(0.01)

            async with anyio.create_task_group() as tg:
                tg.start_soon(tick)
                with pytest.raises(TimeoutError), anyio.fail_after(0.3):
                    await conn.call("echo", (b"x" * (8 * 1024 * 1024),), {})
                tg.cancel_scope.cancel()
            assert ticks > 5
            # Half a frame went out, so the channel is out of step and closed.
            await anyio.wait_all_tasks_blocked()
            with pytest.raises(EOFError):
                await conn.call("echo", (1,), {})

    run(main)


@pytest.mark.parametrize(
    ("frame", "message"),
    [
        (struct.pack("!LB", arpyc.MAX_FRAME + 1, 0), "over the"),
        (struct.pack("!LB", 3, 0) + b"abcX", "flusher"),
        (struct.pack("!LB", 3, 0) + b"abc\n", "unreadable frame"),
    ],
)
def test_an_unreadable_frame_reaches_the_pending_call(frame: bytes, message: str) -> None:
    async def main() -> None:
        async with raw() as (conn, theirs):

            async def ask() -> None:
                with anyio.fail_after(2), pytest.raises(ProtocolError, match=message):
                    await conn.call("echo", (1,), {})

            async with anyio.create_task_group() as tg:
                tg.start_soon(ask)
                await anyio.wait_all_tasks_blocked()
                theirs.sendall(frame)
            assert conn.closed

    run(main)


def test_a_cancelled_call_leaves_no_pending_entry() -> None:
    async def main() -> None:
        async with pair(Echo()) as (conn, _):
            with anyio.move_on_after(0.05):
                await conn.call("hang", (), {})
            assert conn._pending == {}

    run(main)


def test_close_cancels_the_answers_in_flight() -> None:
    async def main() -> None:
        service = Echo()
        async with pair(service) as (conn, peer), anyio.create_task_group() as tg:

            async def ask() -> None:
                with pytest.raises(EOFError):
                    await conn.call("hang", (), {})

            tg.start_soon(ask)
            await service.started.wait()
            peer.close()
            with anyio.fail_after(1):
                await service.cancelled.wait()

    run(main)
