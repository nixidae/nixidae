"""
The frames, the build identity, the peer check and faults (nixidae/nixidae#714).

Over a socketpair: what crosses is real bytes on a real Unix socket,
and no socket path is involved.
"""

import os
import socket
import struct
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from anyio.abc import SocketStream, UNIXSocketStream
from conftest import socket_path

from huggorm.protocol import (
    MAX_FRAME,
    Channel,
    ConnectionLost,
    Control,
    Faults,
    Op,
    Peer,
    ProtocolError,
    Refusal,
    Refused,
    build_identity,
    peer_uid,
)


@pytest.fixture
async def pair() -> AsyncIterator[tuple[Channel, Channel]]:
    a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    left = Channel(await UNIXSocketStream.from_socket(a))
    right = Channel(await UNIXSocketStream.from_socket(b))
    try:
        yield left, right
    finally:
        await left.aclose()
        await right.aclose()


@pytest.mark.anyio
async def test_a_frame_crosses_whole(pair: tuple[Channel, Channel]) -> None:
    left, right = pair
    sent = [Op.CALL, 7, 3, "h1", [b"\x00\xff", "text", None, {"k": [1.5]}]]
    await left.send(sent)
    got = await right.receive()
    assert got == sent and got[0] is Op.CALL


@pytest.mark.anyio
async def test_frames_keep_their_boundaries(
        pair: tuple[Channel, Channel]) -> None:
    """Fifty senders at once, each frame larger than the last. The
    reader runs beside them: the socket buffer holds far less than
    all fifty."""
    left, right = pair
    seen: list[int] = []

    async def read() -> None:
        for _ in range(50):
            frame = await right.receive()
            assert frame[2] == "x" * (frame[1] * 997)
            seen.append(frame[1])

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(read)
            for i in range(50):
                tg.start_soon(left.send, [Op.RESULT, i, "x" * (i * 997)])
    assert sorted(seen) == list(range(50))


@pytest.mark.anyio
async def test_a_bad_frame_is_refused(pair: tuple[Channel, Channel]) -> None:
    left, right = pair
    await left.stream.send(struct.pack(">I", MAX_FRAME + 1))
    with pytest.raises(ProtocolError, match="over the"):
        await right.receive()


@pytest.mark.anyio
async def test_a_close_between_frames_is_the_end(
        pair: tuple[Channel, Channel]) -> None:
    """Both ends stop reading on EndOfStream, so a clean close must
    read as one."""
    left, right = pair
    await left.send([Op.DROP, []])
    await left.stream.send_eof()
    assert await right.receive() == [Op.DROP, []]
    with pytest.raises(anyio.EndOfStream):
        await right.receive()


@pytest.mark.anyio
async def test_an_unknown_op_is_refused(pair: tuple[Channel, Channel]) -> None:
    left, right = pair
    await left.send([99, 1])
    with pytest.raises(ProtocolError, match="no op numbered 99"):
        await right.receive()


@pytest.mark.anyio
async def test_the_peer_is_this_uid(pair: tuple[Channel, Channel]) -> None:
    left, _ = pair
    assert peer_uid(left.stream) == os.geteuid()


@pytest.mark.anyio
async def test_a_late_answer_is_dropped() -> None:
    """An answer for a call nobody waits on - one the client cancelled -
    is dropped, and the next answer still reaches its own call.

    A fake server, because a real one only sends a late answer in a
    race."""
    from huggorm import remote

    path = socket_path()

    async def fake(stream: SocketStream) -> None:
        channel = Channel(stream)
        await channel.receive()
        await channel.send([Op.WELCOME, "token", 0.0])
        call = await channel.receive()
        await channel.send([Op.RESULT, 999, "stray"])
        await channel.send([Op.RESULT, call[1], True])
        with pytest.raises(anyio.EndOfStream):
            await channel.receive()

    listener = await anyio.create_unix_listener(path)
    async with anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, fake, None)
        with anyio.fail_after(10):
            async with remote.connect(path) as client:
                assert await client._ask(Op.CONTROL, Control.PING, []) is True
        tg.cancel_scope.cancel()


def test_each_direction_asks_and_answers_with_the_same_ops() -> None:
    """A callback from the server is a CALL, answered by RESULT or
    FAULT, as a call from the client is."""
    assert {op.name for op in Op} == {
        "HELLO", "WELCOME", "CALL", "CONTROL", "CANCEL", "DROP",
        "RESULT", "FAULT"}


@pytest.mark.anyio
async def test_an_answer_reaches_its_asker(
        pair: tuple[Channel, Channel]) -> None:
    """A RESULT answers its value, a FAULT raises."""
    left, right = pair
    asker = Peer(left, Faults())

    async def server() -> None:
        call = await right.receive()
        await right.send([Op.RESULT, call[1], "value"])
        call = await right.receive()
        await right.send([Op.FAULT, call[1], Faults().encode(
            Refused(Refusal.SWEPT, "gone"))])

    async def reader() -> None:
        while True:
            asker.deliver(await left.receive())

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(server)
            tg.start_soon(reader)
            assert await asker.ask(Op.CALL, 1, "read", []) == "value"
            with pytest.raises(Refused, match="gone"):
                await asker.ask(Op.CONTROL, Control.PING, [])
            tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_closed_peer_says_so_and_refuses_to_ask(
        pair: tuple[Channel, Channel]) -> None:
    peer = Peer(pair[0], Faults())
    assert not peer.closed.is_set()
    peer.close()
    with anyio.fail_after(1):
        await peer.closed.wait()
    with pytest.raises(ConnectionLost):
        await peer.ask(Op.CONTROL, Control.PING, [])


@pytest.mark.anyio
async def test_an_asker_that_stops_waiting_sends_cancel(
        pair: tuple[Channel, Channel]) -> None:
    left, right = pair
    asker = Peer(left, Faults())
    with anyio.move_on_after(0.2):
        await asker.ask(Op.CONTROL, Control.PING, [])
    with anyio.fail_after(10):
        call = await right.receive()
        assert await right.receive() == [Op.CANCEL, call[1]]


@pytest.mark.anyio
async def test_close_wakes_every_asker_and_refuses_the_next(
        pair: tuple[Channel, Channel]) -> None:
    left, right = pair
    asker = Peer(left, Faults())

    async def waits() -> None:
        with pytest.raises(ConnectionLost):
            await asker.ask(Op.CONTROL, Control.PING, [])

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            tg.start_soon(waits)
            await right.receive()
            asker.close()
    with pytest.raises(ConnectionLost):
        await asker.ask(Op.CONTROL, Control.PING, [])


@pytest.mark.anyio
async def test_a_cancel_reaches_an_answer_that_has_not_started(
        pair: tuple[Channel, Channel]) -> None:
    """A CANCEL read right after its request finds the answer's scope,
    even when the answer's task has not run yet. Nothing in between
    has to checkpoint: `serve` registers the scope before it starts
    the task."""
    left, right = pair
    answerer = Peer(left, Faults())
    ran = False

    async def answer() -> list[Any]:
        nonlocal ran
        ran = True
        return [Op.RESULT, 1, "late"]

    async with anyio.create_task_group() as tg:
        answerer.serve(tg, 1, answer)
        answerer.cancel(1)
    assert not ran
    await left.stream.send_eof()
    with pytest.raises(anyio.EndOfStream):
        await right.receive()


@pytest.mark.anyio
async def test_close_cancels_every_answer(
        pair: tuple[Channel, Channel]) -> None:
    left, _ = pair
    answerer = Peer(left, Faults())
    stopped = anyio.Event()

    async def answer() -> list[Any]:
        try:
            await anyio.sleep_forever()
        finally:
            stopped.set()
        raise AssertionError("unreachable")

    with anyio.fail_after(10):
        async with anyio.create_task_group() as tg:
            answerer.serve(tg, 1, answer)
            await anyio.sleep(0)
            answerer.close()
    assert stopped.is_set()


def test_the_identity_is_stable() -> None:
    first = build_identity()
    assert first == build_identity() and len(first) == 64


def test_a_declared_error_crosses_as_itself() -> None:
    """Not `str()` of each part: `info` is a record, and its parts
    cross as one (nixidae/nixidae#637)."""
    from huggorm_bindings import EvalState, Store
    from huggorm_bindings.errors import MissingAttribute

    with pytest.raises(MissingAttribute) as caught:
        EvalState(Store("dummy://")).eval_expr("{ foo = 1; }").get("fo")
    sent = caught.value
    faults = Faults()
    from huggorm.codec import pack, unpack

    rebuilt = faults.decode(unpack(pack(faults.encode(sent))))
    assert isinstance(rebuilt, MissingAttribute)
    assert rebuilt.info is not None and rebuilt == sent


def test_an_internal_error_keeps_its_cause() -> None:
    from huggorm_generated._runtime import InternalError

    faults = Faults()
    sent = InternalError("Store.get_uri failed", cause=KeyError("h1"))
    rebuilt = faults.decode(faults.encode(sent))
    assert type(rebuilt) is InternalError
    assert str(rebuilt) == "Store.get_uri failed"
    assert type(rebuilt.__cause__) is KeyError
    # A name outside builtins never reaches a constructor.
    odd = faults.decode(["internal", "m", "os.system", "x", None, None])
    assert type(odd.__cause__) is Exception
