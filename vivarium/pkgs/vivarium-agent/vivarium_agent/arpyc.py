"""Minimal async RPC over a raw fd, wire-compatible with rpyc.

rpyc's own ``Connection`` is thread-based and costs a round trip per
attribute access.  This keeps rpyc's framing and serialisation (brine for
values, vinegar for exceptions) but drives it from anyio, and adds a
single handler that calls a service method by name so that a call is
exactly one round trip::

    rc, out, err = await conn.root.run("hostname")

Frame:    [4B BE length][1B compressed flag][payload][b"\\n"]
Payload:  [1B message type] + brine.dump((seq, args))
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import struct
import termios
import tty
import zlib
from collections.abc import Awaitable
from typing import TYPE_CHECKING, Any, cast

import anyio
from rpyc.core import brine, consts, vinegar

if TYPE_CHECKING:
    from anyio.abc import TaskGroup, TaskStatus

HANDLE_METHOD = 100
"""Custom handler: args are (method_name, args, kwargs_items).

The server calls ``exposed_<method_name>(*args, **dict(kwargs_items))``.
"""

LABEL_DICT = 99
"""Box label for dicts; brine has no dict support and rpyc's own labels
stop at 4."""

type Boxed = tuple[int, Any]
"""A value with its brine label. The payload's shape follows the label,
so it has no static type."""

_FRAME_HEADER = struct.Struct("!LB")
_FLUSHER = b"\n"
_READ_CHUNK = 65536

MAX_FRAME = 256 * 1024 * 1024
"""The longest frame a peer may announce. A length past it is a stream out
of step, and reading it would buffer up to 4 GiB first."""


class ProtocolError(Exception):
    """The peer sent a frame that cannot be read. The channel is closed."""


def _dump_exc(exc: BaseException) -> tuple[object, ...]:
    """Serialise an exception; vinegar takes every flag positionally."""
    # rpyc is untyped, so vinegar.dump returns Any.
    return cast("tuple[object, ...]", vinegar.dump(type(exc), exc, exc.__traceback__, True, True))


class UnbuiltError(Exception):
    """A peer's exception that vinegar cannot rebuild here."""

    def __init__(self, text: str, remote_tb: str) -> None:
        super().__init__(text)
        self._remote_tb = remote_tb
        """Where vinegar keeps a peer's traceback, and rpyc's tooling reads it."""


def _load_exc(raw: tuple[object, ...]) -> BaseException:
    """Rebuild a serialised exception, refusing to import guest modules.

    One vinegar cannot rebuild arrives as `UnbuiltError` with its name,
    arguments and remote traceback. An `ExceptionGroup` is one:
    vinegar calls `cls.__new__(cls)`, which a group refuses. Raised here,
    the error would close the whole channel as an unreadable frame."""
    try:
        # rpyc is untyped, so vinegar.load returns Any.
        return cast("BaseException", vinegar.load(raw, False, False, False))
    except TypeError:
        (modname, clsname), args, _attrs, tbtext = cast("tuple[tuple[str, str], object, object, str]", raw)
        return UnbuiltError(f"{modname}.{clsname}: {args}", tbtext)


class Service:
    """Base class for objects exposed over arpyc.

    Callable methods are named ``exposed_<name>`` and are reached from the
    peer as ``conn.root.<name>(...)``.
    """

    def on_connect(self, conn: AsyncConnection) -> None:
        pass

    def on_disconnect(self, conn: AsyncConnection) -> None:
        pass


class FdStream:
    """Async byte stream over a bidirectional fd.

    This is the one part that stays on asyncio, deliberately: anyio has no
    async stream over a raw fd (a socketpair end, ``/dev/ttyS0``), and
    ``StreamReader`` fed from ``loop.add_reader`` with ``readexactly`` is
    the idiomatic way to frame one. It runs under anyio's asyncio backend
    unchanged.

    Reads are fed into a ``StreamReader`` from the event loop's reader
    callback. The fd is non-blocking, and a write waits for room with
    ``anyio.wait_writable``: a peer that stops reading stalls its own
    calls, not the loop.

    A ``/dev/ttyS0`` fd must be put in raw mode first, otherwise the line
    discipline mangles the binary frames -- pass ``raw_tty=True``.
    """

    def __init__(
        self,
        fd: int,
        loop: asyncio.AbstractEventLoop,
        *,
        raw_tty: bool = False,
    ) -> None:
        self._fd = fd
        self._loop = loop
        self._closed = False
        self._reader = asyncio.StreamReader()
        # One frame at a time: two writers that both wait for room would
        # interleave their bytes.
        self._writing = anyio.Lock()
        self._saved_tty: list[Any] | None = termios.tcgetattr(fd) if raw_tty else None
        if raw_tty:
            # TCSANOW, not tty.setraw's default TCSAFLUSH: this is a data
            # channel, and discarding whatever the peer has already sent
            # loses a request with no way to notice.
            tty.setraw(fd, termios.TCSANOW)
        os.set_blocking(fd, False)
        loop.add_reader(fd, self._on_readable)

    def _on_readable(self) -> None:
        try:
            data = os.read(self._fd, _READ_CHUNK)
        except BlockingIOError:
            return
        except OSError:
            data = b""
        if data:
            self._reader.feed_data(data)
        else:
            self._reader.feed_eof()

    async def read(self, count: int) -> bytes:
        return await self._reader.readexactly(count)

    async def write(self, data: bytes) -> None:
        """Write all of *data*, or close the stream: half a frame puts the peer out of step."""
        async with self._writing:
            view = memoryview(data)
            try:
                while view:
                    if self._closed:
                        raise EOFError("arpyc stream closed")
                    try:
                        view = view[os.write(self._fd, view) :]
                    except BlockingIOError:
                        try:
                            await anyio.wait_writable(self._fd)
                        except anyio.ClosedResourceError:
                            raise EOFError("arpyc stream closed") from None
            finally:
                if 0 < len(view) < len(data):
                    self.close()

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._loop.remove_reader(self._fd)
        # Wakes a write waiting for room, which then raises EOFError.
        anyio.notify_closing(self._fd)
        self._reader.feed_eof()
        if self._saved_tty is not None:
            with contextlib.suppress(termios.error):
                termios.tcsetattr(self._fd, termios.TCSANOW, self._saved_tty)
        os.close(self._fd)


class Channel:
    """rpyc's framing, with async I/O."""

    def __init__(self, stream: FdStream) -> None:
        self._stream = stream

    async def recv(self) -> bytes:
        header = await self._stream.read(_FRAME_HEADER.size)
        length, compressed = _FRAME_HEADER.unpack(header)
        if length > MAX_FRAME:
            raise ProtocolError(f"a frame of {length} bytes is over the {MAX_FRAME}-byte limit")
        data = await self._stream.read(length + len(_FLUSHER))
        if not data.endswith(_FLUSHER):
            raise ProtocolError("a frame does not end in rpyc's flusher byte")
        data = data[: -len(_FLUSHER)]
        return zlib.decompress(data) if compressed else data

    async def send(self, data: bytes) -> None:
        await self._stream.write(_FRAME_HEADER.pack(len(data), 0) + data + _FLUSHER)

    @property
    def closed(self) -> bool:
        return self._stream.closed

    def close(self) -> None:
        self._stream.close()


class _RemoteMethod:
    __slots__ = ("_conn", "_name")

    def __init__(self, conn: AsyncConnection, name: str) -> None:
        self._conn = conn
        self._name = name

    async def __call__(self, *args: object, **kwargs: object) -> Any:
        return await self._conn.call(self._name, args, kwargs)


class _RemoteRoot:
    """Turns ``conn.root.foo(...)`` into a call of the peer's
    ``exposed_foo``."""

    __slots__ = ("_conn",)

    def __init__(self, conn: AsyncConnection) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> _RemoteMethod:
        if name.startswith("_"):
            raise AttributeError(name)
        return _RemoteMethod(self._conn, name)


class _Pending:
    """One in-flight call, waiting for its reply. anyio's answer to a
    Future: the serve task fills `result` or `exc` and sets the event."""

    __slots__ = ("event", "exc", "result")

    def __init__(self) -> None:
        self.event = anyio.Event()
        # The reply's shape is set by the method the peer ran.
        self.result: Any = None
        self.exc: BaseException | None = None


class AsyncConnection:
    """A symmetric arpyc endpoint: serves *service* and calls the peer's."""

    def __init__(self, service: Service, channel: Channel) -> None:
        self._channel = channel
        self._service = service
        self._seq = 0
        self._pending: dict[int, _Pending] = {}
        self._closed = False
        # Set while `serve_forever` runs: each answer to a peer's call runs in it.
        self._answers: TaskGroup | None = None
        self.root = _RemoteRoot(self)
        service.on_connect(self)

    # ── serialisation ──────────────────────────────────────────────

    def _box(self, obj: object) -> Boxed:
        """Wrap *obj* in a label so brine can carry lists and dicts."""
        if brine.dumpable(obj):
            return consts.LABEL_VALUE, obj
        if isinstance(obj, (tuple, list)):
            return consts.LABEL_TUPLE, tuple(self._box(item) for item in obj)
        if isinstance(obj, dict):
            return LABEL_DICT, tuple((k, self._box(v)) for k, v in obj.items())
        return consts.LABEL_VALUE, obj

    def _unbox(self, package: Boxed) -> Any:
        label, value = package
        if label == consts.LABEL_TUPLE:
            return tuple(self._unbox(item) for item in value)
        if label == LABEL_DICT:
            return {k: self._unbox(v) for k, v in value}
        return value

    @staticmethod
    def _frame(msg_type: int, seq: int, data: Any) -> bytes:
        # rpyc is untyped, so both halves arrive as Any.
        header: bytes = brine.I1.pack(msg_type)
        body: bytes = brine.dump((seq, data))
        return header + body

    async def _send(self, frame: bytes) -> None:
        if self._closed:
            raise EOFError("arpyc connection closed")
        await self._channel.send(frame)

    # ── serving ────────────────────────────────────────────────────

    async def _dispatch(self, seq: int, args: tuple[int, Boxed]) -> None:
        handler_id, boxed_args = args
        try:
            if handler_id == HANDLE_METHOD:
                method, call_args, kwargs = self._unbox(boxed_args)
                func = getattr(self._service, "exposed_" + method)
                result = func(*call_args, **dict(kwargs))
                if inspect.isawaitable(result):
                    # Answered from its own task: awaited here, a slow call
                    # (a guest asking the host to boot another) would stop
                    # this loop reading the replies the host waits for.
                    if self._answers is None:
                        raise RuntimeError("a request arrived with nothing serving the connection")
                    self._answers.start_soon(self._answer, seq, result)
                    return
            elif handler_id == consts.HANDLE_PING:
                result = self._unbox(boxed_args)
            elif handler_id == consts.HANDLE_CLOSE:
                self.close()
                return
            else:
                raise ValueError(f"unknown handler {handler_id}")
        except Exception as exc:  # noqa: BLE001 -- the caller's error goes back to the caller
            await self._send(self._frame(consts.MSG_EXCEPTION, seq, _dump_exc(exc)))
        else:
            await self._send(self._frame(consts.MSG_REPLY, seq, self._box(result)))

    async def _answer(self, seq: int, pending: Awaitable[object]) -> None:
        try:
            result = await pending
            # Inside the try: a result brine cannot dump is the caller's error, not a reply that never comes.
            frame = self._frame(consts.MSG_REPLY, seq, self._box(result))
        except Exception as exc:  # noqa: BLE001 -- the caller's error goes back to the caller
            frame = self._frame(consts.MSG_EXCEPTION, seq, _dump_exc(exc))
        # A peer that went away mid-call has nowhere to take the answer.
        with contextlib.suppress(EOFError, OSError):
            await self._send(frame)

    async def _serve_one(self) -> None:
        """Read one frame and dispatch it."""
        data = await self._channel.recv()
        msg = brine.I1.unpack(data[:1])[0]
        seq, args = brine.load(data[1:])

        if msg == consts.MSG_REQUEST:
            await self._dispatch(seq, args)
            return

        pending = self._pending.pop(seq, None)
        if pending is None or pending.event.is_set():
            return
        if msg == consts.MSG_REPLY:
            pending.result = self._unbox(args)
        elif msg == consts.MSG_EXCEPTION:
            pending.exc = _load_exc(args)
        pending.event.set()

    async def serve_forever(self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        """Serve until the peer goes away, or sends what cannot be read.

        Every call made on the connection waits for a reply this reads, so
        the side that calls out starts it in a task group it holds,
        `await group.start(conn.serve_forever)`, before its first call.

        Never raises for the peer's sake: a listener that serves several
        peers keeps serving the rest.
        """
        async with anyio.create_task_group() as answers:
            self._answers = answers
            task_status.started()
            try:
                await self._read()
            finally:
                self._answers = None
                # Their replies have nowhere to go.
                answers.cancel_scope.cancel()

    async def _read(self) -> None:
        broken: ProtocolError | None = None
        try:
            while not self._closed:
                await self._serve_one()
        except (EOFError, asyncio.IncompleteReadError, OSError):
            pass
        except ProtocolError as error:
            broken = error
        except Exception as error:  # noqa: BLE001 -- brine and zlib raise what they raise; each pending call gets it
            broken = ProtocolError(f"an unreadable frame: {type(error).__name__}: {error}")
        finally:
            self.close(broken)

    # ── calling ────────────────────────────────────────────────────

    async def call(self, method: str, args: tuple[object, ...], kwargs: dict[str, object]) -> Any:
        """Invoke ``exposed_<method>`` on the peer and await its result."""
        if self._closed:
            raise EOFError("arpyc connection closed")
        if self._answers is None:
            # Its reply would never be read.
            raise RuntimeError("arpyc: nothing serves this connection; start serve_forever first")
        seq = self._seq
        self._seq += 1
        pending = _Pending()
        self._pending[seq] = pending
        try:
            boxed = self._box((method, args, tuple(kwargs.items())))
            await self._send(self._frame(consts.MSG_REQUEST, seq, (HANDLE_METHOD, boxed)))
            await pending.event.wait()
        finally:
            # A cancelled call leaves no entry behind.
            self._pending.pop(seq, None)
        if pending.exc is not None:
            raise pending.exc
        return pending.result

    # ── lifecycle ──────────────────────────────────────────────────

    def close(self, broken: ProtocolError | None = None) -> None:
        """Close the channel. Each pending call raises *broken*, or EOFError."""
        if self._closed:
            return
        self._closed = True
        self._service.on_disconnect(self)
        for pending in self._pending.values():
            if not pending.event.is_set():
                pending.exc = ProtocolError(str(broken)) if broken else EOFError("arpyc connection closed")
                pending.event.set()
        self._pending.clear()
        if self._answers is not None:
            # Ends `serve_forever` and the answers in flight: their replies have nowhere to go.
            self._answers.cancel_scope.cancel()
        self._channel.close()

    @property
    def closed(self) -> bool:
        return self._closed


def _connection(service: Service, fd: int, *, raw_tty: bool) -> AsyncConnection:
    loop = asyncio.get_running_loop()
    return AsyncConnection(service, Channel(FdStream(fd, loop, raw_tty=raw_tty)))


def connect(fd: int, service: Service | None = None) -> AsyncConnection:
    """Client side: talk to a guest agent over *fd* (a socketpair end), and answer it with *service*.

    Start `serve_forever` in a task group the caller holds before the first call.
    """
    return _connection(service or Service(), fd, raw_tty=False)


def listen(fd: int, service: Service, *, raw_tty: bool = True) -> AsyncConnection:
    """Guest side: put *fd* (``/dev/ttyS0``) in raw mode and start
    reading it.  Call ``serve_forever`` on the result to answer.

    Reading starts here rather than in ``serve_forever`` so that a guest
    can announce itself and be certain that nothing sent afterwards is
    missed: from this point requests queue up in the stream.

    ``raw_tty=False`` for a socket, which has no line discipline to turn
    off and fails ``tcgetattr``.
    """
    return _connection(service, fd, raw_tty=raw_tty)
