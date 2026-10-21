"""
The frames huggorm speaks on its Unix socket.

A frame is a u32 big-endian length, then a msgpack array whose first
element is an `Op`. Values inside a frame are `codec` shapes.

Both ends run the same build, so nothing here negotiates. HELLO
carries the build identity, and a server refuses any other
(nixidae/nixidae#714). Only the server's own uid may connect.

    HELLO     [Op, identity, claim | None]
    WELCOME   [Op, token, lease_ttl]
    CALL      [Op, call_id, index, handle | None, [arg, ...]]
    CONTROL   [Op, call_id, Control, [arg, ...]]
    CANCEL    [Op, call_id]
    DROP      [Op, [handle, ...]]                     one way
    RESULT    [Op, call_id, value]
    FAULT     [Op, call_id | None, fault]

With experimental callbacks on (nixidae/nixidae#738), the server
calls an object the client keeps. It asks with a CALL of its own body,
and the client answers with RESULT or FAULT:

    CALL      [Op, call_id, client_id, method, [arg, ...]]

Each end reads only the CALLs the other end sends, so the two bodies
never meet one reader. The server sends CANCEL when the call that
waits for a callback is cancelled. The client cancels that hook and
answers nothing.

`call_id` is the asker's, and a RESULT or FAULT carries it back, so
calls on one connection may finish in any order. A CALL names its call
by its index in the emitted `CALLS`. A FAULT with no `call_id` answers
the HELLO, and the server closes the connection after it.

A CANCEL gets no answer. A late RESULT for a cancelled call can still
arrive, and the client drops it.
"""

from __future__ import annotations

import builtins
import contextlib
import enum
import functools
import hashlib
import importlib
import importlib.util
import itertools
import pathlib
import socket
import struct
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

import anyio
import anyio.lowlevel
from anyio.abc import SocketAttribute, SocketStream, TaskGroup
from anyio.streams.buffered import BufferedByteReceiveStream

from huggorm_generated._callspec import Call, Wire, WireKind
from huggorm_generated._policy import ERROR_FIELDS, ERROR_MODULE, FREE, METHODS

from .codec import Codec, pack, unpack

# A frame larger than this is a bug or an attack. The largest real
# answer is a store's whole path list, far below it.
MAX_FRAME = 256 << 20
_LENGTH = struct.Struct(">I")


def _log_calls() -> tuple[Wire, Call]:
    reader = FREE["subscribe_process_logs"].returns
    assert reader is not None
    return reader, next(c for c in METHODS[reader.name] if c.name == "drain")


# A log reader's class, and the drain whose batch LOGS_NEXT answers.
# Read off the calls, because no layer above the bindings names a
# domain type.
LOG_READER, LOG_DRAIN = _log_calls()


class Op(enum.IntEnum):
    HELLO = 0
    WELCOME = 1
    CALL = 2
    CONTROL = 3
    CANCEL = 4
    DROP = 5
    RESULT = 6
    FAULT = 7


class Control(enum.IntEnum):
    """The session operations: everything that is not a declared call."""

    PING = 0
    SHARE = 1
    DETACH = 2
    RELEASE = 3
    REALIZE = 4
    LOGS_BARRIER = 5
    LOGS = 6
    BUILD = 7
    LOGS_NEXT = 8


class ProtocolError(Exception):
    """The peer sent something this build never writes."""


class Refusal(enum.StrEnum):
    """Why a server will not serve a connection. A fault's `code`."""

    # The HELLO named another build.
    IDENTITY = "refused_identity"
    # The sweeper took this connection's token and every lease under it.
    SWEPT = "refused_swept"


# By value, because `in` on an enum class takes a value only from 3.12.
_REFUSALS = {r.value: r for r in Refusal}


class Refused(Exception):
    """The server will not serve this connection, and says why."""

    def __init__(self, reason: Refusal, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.code = str(reason)
        self.message = message


@functools.cache
def build_identity() -> str:
    """A digest of every Python source both ends run.

    The generated package holds the call table and the value shapes,
    and this package holds the frames and the session. Two ends that
    agree on all of it agree on the protocol, so nothing finer than a
    digest is needed."""
    digest = hashlib.sha256()
    for name in ("huggorm", "huggorm_generated"):
        spec = importlib.util.find_spec(name)
        if spec is None or spec.origin is None:
            raise RuntimeError(f"{name} is not importable")
        root = pathlib.Path(spec.origin).parent
        for path in sorted(root.rglob("*.py")):
            digest.update(str(path.relative_to(root)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
    return digest.hexdigest()


def peer_uid(stream: SocketStream) -> int:
    """The uid of the process on the other end of a Unix socket, as
    the kernel recorded it at connect time."""
    sock: socket.socket = stream.extra(SocketAttribute.raw_socket)
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED,
                            struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return int(uid)


class Channel:
    """Frames over one connected stream. Sends are serialised, because
    calls on one connection answer from many tasks."""

    def __init__(self, stream: SocketStream) -> None:
        self.stream = stream
        self._reader = BufferedByteReceiveStream(stream)
        self._send_lock = anyio.Lock()

    async def send(self, frame: list[Any]) -> None:
        body = pack(frame)
        if len(body) > MAX_FRAME:
            raise ProtocolError(f"a {len(body)} byte frame is over the "
                                f"{MAX_FRAME} byte limit")
        # Shielded: a cancel in the middle of a write leaves the peer
        # reading a body that never ends.
        with anyio.CancelScope(shield=True):
            async with self._send_lock:
                await self.stream.send(_LENGTH.pack(len(body)) + body)

    async def receive(self) -> list[Any]:
        """The next frame, as `[Op, ...]`. Raises EndOfStream when the
        peer closed between frames."""
        # `receive_exactly` reports a close as IncompleteRead, even
        # one that lands between frames.
        try:
            head = await self._reader.receive_exactly(4)
        except anyio.IncompleteRead:
            raise anyio.EndOfStream from None
        (length,) = _LENGTH.unpack(head)
        if length > MAX_FRAME:
            raise ProtocolError(f"a {length} byte frame is over the "
                                f"{MAX_FRAME} byte limit")
        try:
            body = await self._reader.receive_exactly(length)
        except anyio.IncompleteRead:
            raise ProtocolError(
                f"the peer closed inside a {length} byte frame") from None
        frame = unpack(body)
        if type(frame) is not list or not frame or type(frame[0]) is not int:
            raise ProtocolError(f"a frame arrived as {frame!r:.80}")
        try:
            frame[0] = Op(frame[0])
        except ValueError:
            raise ProtocolError(f"no op numbered {frame[0]}") from None
        return frame

    async def aclose(self) -> None:
        await self.stream.aclose()


class ConnectionLost(ConnectionError):
    """The connection closed, so a request has no answer. The server's
    leases are not released by that: a new connection that claims the
    token can take them back while they last."""


@dataclass
class _Waiting:
    """One request waiting for its answer. `frame` stays None when the
    connection closed first."""

    done: anyio.Event = field(default_factory=anyio.Event)
    frame: list[Any] | None = None


class Peer:
    """One end's requests in flight, both ways, over one channel.

    The protocol is symmetric. Each end asks (CALL and CONTROL from the
    client, CALL from the server) and answers what the other asks.
    The ids of each direction are the asker's own. Whoever reads the
    channel hands this object the answers and the CANCELs."""

    def __init__(self, channel: Channel, faults: Faults) -> None:
        self.channel = channel
        self.faults = faults
        self._ids = itertools.count(1)
        self._waiting: dict[int, _Waiting] = {}
        self._answering: dict[int, anyio.CancelScope] = {}
        self.closed = anyio.Event()

    async def ask(self, op: Op, *body: Any) -> Any:
        """Send one request and answer the value of its RESULT, or
        raise the fault of its FAULT.

        An asker that stops waiting for any reason sends CANCEL, so
        the other end stops work nobody will read. The answer may still
        arrive, and `deliver` drops it."""
        if self.closed.is_set():
            raise ConnectionLost("the connection is closed")
        n = next(self._ids)
        waiting = self._waiting[n] = _Waiting()
        try:
            try:
                await self.channel.send([op, n, *body])
            except (anyio.BrokenResourceError,
                    anyio.ClosedResourceError) as e:
                raise ConnectionLost("the connection is closed") from e
            try:
                await waiting.done.wait()
            except BaseException:
                if not waiting.done.is_set():
                    # `send` shields itself, so a cancelled asker still
                    # sends.
                    with contextlib.suppress(anyio.BrokenResourceError,
                                             anyio.ClosedResourceError):
                        await self.channel.send([Op.CANCEL, n])
                raise
        finally:
            self._waiting.pop(n, None)
        match waiting.frame:
            case [Op.RESULT, _, value]:
                return value
            case [Op.FAULT, _, fault]:
                raise self.faults.decode(fault)
            case None:
                raise ConnectionLost("the connection closed during the call")
        raise ProtocolError(f"an answer arrived as {waiting.frame!r:.80}")

    def deliver(self, frame: list[Any]) -> None:
        """An answer: `[op, id, value or fault]`. One for a request
        nobody waits on any more is dropped."""
        waiting = self._waiting.get(frame[1])
        if waiting is not None:
            waiting.frame = frame
            waiting.done.set()

    def serve(self, tasks: TaskGroup, n: int,
              answer: Callable[[], Awaitable[list[Any]]]) -> None:
        """Answer request `n` in a task of `tasks`: send the frame
        `answer` makes. A CANCEL for `n` ends it with no answer.

        The scope is registered before the task starts, so a CANCEL
        read at once finds it. Nothing has to checkpoint in between."""
        scope = self._answering[n] = anyio.CancelScope()
        tasks.start_soon(self._answer, n, scope, answer)

    async def _answer(self, n: int, scope: anyio.CancelScope,
                      answer: Callable[[], Awaitable[list[Any]]]) -> None:
        try:
            with scope:
                # A cancel delivered at the first await would still
                # run `answer` up to that await: a thread may start.
                await anyio.lowlevel.checkpoint_if_cancelled()
                frame = await answer()
                with contextlib.suppress(anyio.BrokenResourceError,
                                         anyio.ClosedResourceError):
                    await self.channel.send(frame)
        finally:
            if self._answering.get(n) is scope:
                del self._answering[n]

    def cancel(self, n: int) -> None:
        """A CANCEL from the other end, for its request `n`."""
        scope = self._answering.get(n)
        if scope is not None:
            scope.cancel()

    def close(self) -> None:
        """The connection ended. Every waiting request fails, then
        every answer stops.

        In this order: an answer can be a thread that waits on one of
        this end's requests, and it ends only once that request fails."""
        self.closed.set()
        for waiting in self._waiting.values():
            waiting.done.set()
        for scope in self._answering.values():
            scope.cancel()


# -- faults --------------------------------------------------------------
_ERRORS = tuple(ERROR_FIELDS)


class Faults:
    """A failure as one msgpack value, and back.

        [code, message, cause_type, cause_message, error, parts]

    A DECLARED error crosses as itself: `error` indexes the declared
    error classes and `parts` are its parts, and the far side rebuilds
    that class, so `except BadStorePath` works over the wire
    (nixidae/nixidae#603). Anything else crosses as an InternalError: its cause
    crosses as itself when declared, and by builtin name when not."""

    def __init__(self, codec: Codec | None = None,
                 module: ModuleType | None = None) -> None:
        self.codec = codec or Codec()
        self._module = module

    @property
    def module(self) -> ModuleType:
        if self._module is None:
            self._module = importlib.import_module(ERROR_MODULE)
        return self._module

    def _declared(self, err: BaseException | None) -> int | None:
        """The index of `err`'s class among the declared errors. By
        identity: a class that only shares a declared name is not it."""
        if err is None:
            return None
        name = type(err).__name__
        if name not in ERROR_FIELDS:
            return None
        if type(err) is not getattr(self.module, name, None):
            return None
        return _ERRORS.index(name)

    def _parts(self, index: int, err: BaseException) -> Any:
        return self.codec.encode(Wire(WireKind.ERROR, _ERRORS[index]), err,
                                 _no_proxy)

    def encode(self, wrapper: Any) -> list[Any]:
        """`wrapper` is a declared error, a WrapperError or a Refused."""
        if isinstance(wrapper, Refused):
            return [wrapper.code, wrapper.message, "", "", None, None]
        itself = self._declared(wrapper)
        if itself is not None:
            return [wrapper.code, wrapper.message, "", "", itself,
                    self._parts(itself, wrapper)]
        cause: BaseException | None = getattr(wrapper, "__cause__", None)
        if cause is None:
            return [wrapper.code, wrapper.message, "", "", None, None]
        declared = self._declared(cause)
        return [wrapper.code, wrapper.message, type(cause).__name__,
                str(cause), declared,
                None if declared is None else self._parts(declared, cause)]

    def decode(self, raw: Any) -> BaseException:
        from huggorm_generated._runtime import InternalError, WrapperError

        if type(raw) is not list or len(raw) != 6:
            raise ProtocolError(f"a fault arrived as {raw!r:.80}")
        code, message, cause_type, cause_message, error, parts = raw
        reason = _REFUSALS.get(code)
        if reason is not None and error is None and not cause_type:
            return Refused(reason, message)
        rebuilt = None
        if error is not None:
            if type(error) is not int or not 0 <= error < len(_ERRORS):
                raise ProtocolError(f"no declared error numbered {error!r}")
            rebuilt = self.codec.decode(
                Wire(WireKind.ERROR, _ERRORS[error]), parts, _no_proxy)
        if not cause_type:
            return rebuilt or WrapperError(message)
        return InternalError(
            message, cause=rebuilt or _approximate(cause_type, cause_message))


def _no_proxy(_: Any) -> Any:
    raise TypeError("an error's part is never a handle")


def _approximate(type_name: str, message: str) -> BaseException:
    """A cause nothing declared, rebuilt from its builtin name.

    The name must resolve in `builtins` AND be an exception class, so
    no other name the peer sends reaches a constructor. One whose
    constructor wants more than a message, such as UnicodeDecodeError,
    becomes an Exception that names it."""
    kls = getattr(builtins, type_name, None)
    if isinstance(kls, type) and issubclass(kls, BaseException):
        try:
            return kls(message)
        except Exception:
            pass
    return Exception(f"{type_name}: {message}" if type_name else message)
