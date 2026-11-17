"""arpyc over a socketpair: a call, and how the channel fails."""

from __future__ import annotations

import socket
import struct
from collections.abc import Awaitable, Callable

import anyio
import pytest
from vivarium_runner import arpyc
from vivarium_runner.arpyc import AsyncConnection, ProtocolError, Service


class Echo(Service):
    def __init__(self) -> None:
        self.cancelled = anyio.Event()
        self.started = anyio.Event()

    def exposed_echo(self, value: object) -> object:
        return value

    async def exposed_hang(self) -> None:
        self.started.set()
        try:
            await anyio.sleep_forever()
        finally:
            self.cancelled.set()


def run(main: Callable[[], Awaitable[None]]) -> None:
    anyio.run(main)


def pair(service: Service) -> tuple[AsyncConnection, AsyncConnection]:
    """A client and a serving peer, each on one end of a socketpair."""
    ours, theirs = socket.socketpair()
    peer = arpyc.listen(theirs.detach(), service, raw_tty=False)
    peer.start_serving()
    return arpyc.connect(ours.detach()), peer


def raw() -> tuple[AsyncConnection, socket.socket]:
    """A client, and the other end as a plain socket the test writes by hand."""
    ours, theirs = socket.socketpair()
    return arpyc.connect(ours.detach()), theirs


def test_a_call_returns_the_peers_answer() -> None:
    async def main() -> None:
        conn, peer = pair(Echo())
        assert await conn.call("echo", ({"a": [1, 2]},), {}) == {"a": (1, 2)}
        conn.close()
        peer.close()

    run(main)


def test_a_call_after_close_raises_at_once() -> None:
    async def main() -> None:
        conn, peer = pair(Echo())
        conn.close()
        with anyio.fail_after(1), pytest.raises(EOFError):
            await conn.call("echo", (1,), {})
        peer.close()

    run(main)


def test_a_peer_that_stops_reading_stalls_the_call_not_the_loop() -> None:
    async def main() -> None:
        conn, theirs = raw()
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
        theirs.close()

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
        conn, theirs = raw()

        async def ask() -> None:
            with anyio.fail_after(2), pytest.raises(ProtocolError, match=message):
                await conn.call("echo", (1,), {})

        async with anyio.create_task_group() as tg:
            tg.start_soon(ask)
            await anyio.wait_all_tasks_blocked()
            theirs.sendall(frame)
        assert conn.closed
        theirs.close()

    run(main)


def test_a_cancelled_call_leaves_no_pending_entry() -> None:
    async def main() -> None:
        conn, peer = pair(Echo())
        with anyio.move_on_after(0.05):
            await conn.call("hang", (), {})
        assert conn._pending == {}
        conn.close()
        peer.close()

    run(main)


def test_close_cancels_the_answers_in_flight() -> None:
    async def main() -> None:
        service = Echo()
        conn, peer = pair(service)
        async with anyio.create_task_group() as tg:

            async def ask() -> None:
                with pytest.raises(EOFError):
                    await conn.call("hang", (), {})

            tg.start_soon(ask)
            await service.started.wait()
            peer.close()
            with anyio.fail_after(1):
                await service.cancelled.wait()
        conn.close()

    run(main)
