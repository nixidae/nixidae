"""The host's proxy: lab destinations through the router's, the rest direct.

`vivarium shell` points `ALL_PROXY` here. The router's proxy reaches only
the lab, so a shell aimed at it alone could fetch nothing public; this
one connects to a public host from the host itself, and works offline.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass

import anyio
import anyio.abc
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.stapled import StapledByteStream

from . import protocol
from .protocol import ProtocolError, Reply, Request
from .resolve import IPAddress, servers_for
from .server import CONNECT_TIMEOUT, Refused

type Network = ipaddress.IPv4Network | ipaddress.IPv6Network

UPSTREAM_TIMEOUT = 2 * CONNECT_TIMEOUT
"""The router's proxy resolves and connects within its own timeouts first."""


@dataclass(frozen=True, slots=True)
class Lab:
    domains: tuple[str, ...]
    """Names under these resolve in the lab."""
    networks: tuple[Network, ...]
    """Addresses in these are the lab's."""

    def holds(self, host: IPAddress | str) -> bool:
        if not isinstance(host, str):
            address = host
        else:
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                return servers_for(host, dict.fromkeys(self.domains, ())) is not None
        return any(address in network for network in self.networks)


@dataclass(frozen=True, slots=True)
class Split:
    lab: Lab
    upstream: tuple[str, int]
    """The host's forward of the router's proxy."""

    async def open(self, request: Request) -> anyio.abc.ByteStream:
        if self.lab.holds(request.host):
            return await through(self.upstream, request)
        with anyio.fail_after(CONNECT_TIMEOUT):
            return await anyio.connect_tcp(str(request.host), request.port)


async def through(upstream: tuple[str, int], request: Request) -> anyio.abc.ByteStream:
    """A CONNECT through the SOCKS5 proxy at *upstream*: the stream once it
    answered, with what it sent after its reply."""
    host, port = upstream
    with anyio.fail_after(UPSTREAM_TIMEOUT):
        stream = await anyio.connect_tcp(host, port)
        try:
            reader = BufferedByteReceiveStream(stream)
            await stream.send(protocol.GREETING)
            version, method = await reader.receive_exactly(2)
            protocol.check_version(version)
            if method != protocol.NO_AUTHENTICATION:
                raise Refused("the lab's proxy takes no client without authentication", Reply.GENERAL_FAILURE)
            await stream.send(protocol.connect_request(request))
            code, kind = protocol.parse_reply_head(await reader.receive_exactly(4))
            first = await reader.receive_exactly(1)
            await reader.receive_exactly(protocol.address_length(kind, first[0]) - 1 + 2)
            if code is not Reply.SUCCEEDED:
                raise Refused(f"the lab's proxy answered {code.name}", code)
        except ProtocolError as error:
            await stream.aclose()
            raise Refused(f"the lab's proxy: {error}", error.reply) from None
        except (anyio.IncompleteRead, anyio.EndOfStream):
            await stream.aclose()
            raise Refused("the lab's proxy closed the connection", Reply.GENERAL_FAILURE) from None
        except BaseException:
            await stream.aclose()
            raise
    return StapledByteStream(stream, reader)
