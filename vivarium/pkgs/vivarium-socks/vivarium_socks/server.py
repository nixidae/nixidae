"""The proxy: accept, read a CONNECT, connect, carry bytes both ways.

What "connect" means is a `Connector`'s: on the router, resolve in the
lab and connect (`Resolving`); on the host, `split.Split`.
"""

from __future__ import annotations

import contextlib
import errno
import ipaddress
import logging
from collections.abc import Sequence
from typing import Protocol

import anyio
import anyio.abc
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.stapled import StapledByteStream

from . import protocol
from .protocol import ProtocolError, Reply, Request
from .resolve import IPAddress, NameResolver, Unresolved

_log = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT = 10.0
CONNECT_TIMEOUT = 10.0
CHUNK = 65536


class Refused(Exception):
    """A connect that failed with a SOCKS reply the client should get as it is."""

    def __init__(self, message: str, reply: Reply) -> None:
        super().__init__(message)
        self.reply = reply


class Connector(Protocol):
    async def open(self, request: Request) -> anyio.abc.ByteStream:
        """A stream to *request*'s destination. Raises `Unresolved`, `Refused` or OSError."""
        ...


async def read_request(stream: anyio.abc.ByteStream) -> tuple[StapledByteStream, Request]:
    """The greeting and the request. Returns *stream* behind the buffer the
    reads used, so no byte the client sent early is lost."""
    reader = BufferedByteReceiveStream(stream)
    version, count = await reader.receive_exactly(2)
    protocol.check_version(version)
    offered = await reader.receive_exactly(count)
    answer = protocol.method_reply(offered)
    await stream.send(answer)
    if answer[1] == protocol.NO_ACCEPTABLE_METHOD:
        raise ProtocolError("the client offers no method without authentication")
    _command, kind = protocol.parse_request_head(await reader.receive_exactly(4))
    first = await reader.receive_exactly(1)
    rest = await reader.receive_exactly(protocol.address_length(kind, first[0]) - 1)
    port = await reader.receive_exactly(2)
    return StapledByteStream(stream, reader), protocol.parse_destination(kind, first + rest, port)


async def addresses(request: Request, resolver: NameResolver) -> Sequence[IPAddress]:
    if not isinstance(request.host, str):
        return [request.host]
    try:
        return [ipaddress.ip_address(request.host)]
    except ValueError:
        return await resolver.resolve(request.host)


async def connect(targets: Sequence[IPAddress], port: int) -> anyio.abc.SocketStream:
    """The first of *targets* that accepts. Raises the last failure."""
    failure: OSError | None = None
    for target in targets:
        try:
            with anyio.fail_after(CONNECT_TIMEOUT):
                return await anyio.connect_tcp(str(target), port)
        except (OSError, TimeoutError) as error:
            failure = error if isinstance(error, OSError) else OSError(f"{target}:{port}: timed out")
    raise failure or OSError("no address")


class Resolving:
    """The router's connector: names from the lab's servers, then a direct connect."""

    def __init__(self, resolver: NameResolver) -> None:
        self.resolver = resolver

    async def open(self, request: Request) -> anyio.abc.ByteStream:
        return await connect(await addresses(request, self.resolver), request.port)


def failure_reply(error: BaseException) -> Reply:
    """The reply for a failed connect. anyio's `connect_tcp` raises a plain
    OSError ("All connection attempts failed") with the socket's error as
    its cause, so the cause chain decides."""
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, OSError) and cause.errno is not None:
            match cause.errno:
                case errno.ECONNREFUSED:
                    return Reply.CONNECTION_REFUSED
                case errno.ENETUNREACH:
                    return Reply.NETWORK_UNREACHABLE
                case _:
                    return Reply.HOST_UNREACHABLE
        cause = cause.__cause__
    return Reply.HOST_UNREACHABLE


async def pump(source: anyio.abc.ByteReceiveStream, sink: anyio.abc.ByteStream) -> None:
    # Either side may close or break at any time; that ends this direction.
    with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError, ConnectionError):
        async for chunk in source:
            await sink.send(chunk)
    with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError, OSError):
        await sink.send_eof()


async def handle(client: anyio.abc.SocketStream, connector: Connector) -> None:
    async with client:
        try:
            with anyio.fail_after(HANDSHAKE_TIMEOUT):
                stream, request = await read_request(client)
        except ProtocolError as error:
            _log.info("refused: %s", error)
            await _send_quietly(client, protocol.reply(error.reply))
            return
        except (anyio.IncompleteRead, anyio.EndOfStream, TimeoutError, ConnectionError):
            return
        try:
            upstream = await connector.open(request)
        except Unresolved as error:
            _log.info("%s", error)
            await _send_quietly(client, protocol.reply(Reply.HOST_UNREACHABLE))
            return
        except Refused as error:
            _log.info("connect %s:%d: %s", request.host, request.port, error)
            await _send_quietly(client, protocol.reply(error.reply))
            return
        except OSError as error:
            _log.info("connect %s:%d: %s", request.host, request.port, error)
            await _send_quietly(client, protocol.reply(failure_reply(error)))
            return
        async with upstream:
            local = upstream.extra(anyio.abc.SocketAttribute.local_address, None)
            bound = ipaddress.ip_address(local[0]) if isinstance(local, tuple) else None
            port = int(local[1]) if isinstance(local, tuple) else 0
            await client.send(protocol.reply(Reply.SUCCEEDED, bound, port))
            async with anyio.create_task_group() as group:
                group.start_soon(pump, stream, upstream)
                group.start_soon(pump, upstream, stream)


async def _send_quietly(client: anyio.abc.ByteStream, data: bytes) -> None:
    # The client may be gone already; a refusal it never reads changes nothing.
    try:
        await client.send(data)
    except (anyio.BrokenResourceError, anyio.ClosedResourceError, ConnectionError):
        _log.debug("the client left before the reply")


async def serve(
    host: str,
    port: int,
    connector: Connector,
    *,
    task_status: anyio.abc.TaskStatus[int] = anyio.TASK_STATUS_IGNORED,
) -> None:
    """Serve until cancelled. Reports the bound port through *task_status*."""
    listener = await anyio.create_tcp_listener(local_host=host, local_port=port, reuse_port=False)
    async with listener:
        bound = listener.extra(anyio.abc.SocketAttribute.local_port)
        _log.info("listening on %s:%d", host, bound)
        task_status.started(bound)

        async def one(client: anyio.abc.SocketStream) -> None:
            await handle(client, connector)

        await listener.serve(one)
