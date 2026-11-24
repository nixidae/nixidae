"""SOCKS5 (RFC 1928) as values: what a client sent, what to answer.

No I/O here; `server` reads and writes the bytes. Only what a proxy into a
lab needs: no authentication, CONNECT only.
"""

from __future__ import annotations

import enum
import ipaddress
from dataclasses import dataclass
from typing import Final

VERSION: Final = 5
NO_AUTHENTICATION: Final = 0x00
NO_ACCEPTABLE_METHOD: Final = 0xFF


class Command(enum.IntEnum):
    CONNECT = 0x01
    BIND = 0x02
    UDP_ASSOCIATE = 0x03


class AddressType(enum.IntEnum):
    IPV4 = 0x01
    DOMAIN = 0x03
    IPV6 = 0x04


class Reply(enum.IntEnum):
    SUCCEEDED = 0x00
    GENERAL_FAILURE = 0x01
    NOT_ALLOWED = 0x02
    NETWORK_UNREACHABLE = 0x03
    HOST_UNREACHABLE = 0x04
    CONNECTION_REFUSED = 0x05
    TTL_EXPIRED = 0x06
    COMMAND_NOT_SUPPORTED = 0x07
    ADDRESS_TYPE_NOT_SUPPORTED = 0x08


class ProtocolError(Exception):
    """The client broke the protocol; the proxy closes the connection."""

    def __init__(self, message: str, reply: Reply = Reply.GENERAL_FAILURE) -> None:
        super().__init__(message)
        self.reply = reply


@dataclass(frozen=True, slots=True)
class Request:
    """A CONNECT to *host*, an IP address or a name the proxy resolves."""

    host: ipaddress.IPv4Address | ipaddress.IPv6Address | str
    port: int


def method_reply(offered: bytes) -> bytes:
    """The answer to a greeting that offered the methods *offered*."""
    chosen = NO_AUTHENTICATION if NO_AUTHENTICATION in offered else NO_ACCEPTABLE_METHOD
    return bytes([VERSION, chosen])


def check_version(version: int) -> None:
    if version != VERSION:
        raise ProtocolError(f"SOCKS version {version}, not {VERSION}")


def parse_request_head(head: bytes) -> tuple[Command, AddressType]:
    """The first four bytes of a request: version, command, reserved, type."""
    version, command, _reserved, kind = head
    check_version(version)
    try:
        parsed = Command(command)
    except ValueError:
        raise ProtocolError(f"command {command:#x}", Reply.COMMAND_NOT_SUPPORTED) from None
    if parsed is not Command.CONNECT:
        raise ProtocolError(f"{parsed.name} is not supported", Reply.COMMAND_NOT_SUPPORTED)
    try:
        return parsed, AddressType(kind)
    except ValueError:
        raise ProtocolError(f"address type {kind:#x}", Reply.ADDRESS_TYPE_NOT_SUPPORTED) from None


def address_length(kind: AddressType, first: int) -> int:
    """How many bytes follow the type byte before the port. *first* is the
    byte after the type: a domain's length."""
    match kind:
        case AddressType.IPV4:
            return 4
        case AddressType.IPV6:
            return 16
        case AddressType.DOMAIN:
            return 1 + first


def parse_destination(kind: AddressType, address: bytes, port: bytes) -> Request:
    """*address* as read after the type byte (a domain with its length byte)."""
    number = int.from_bytes(port, "big")
    match kind:
        case AddressType.IPV4:
            return Request(ipaddress.IPv4Address(address), number)
        case AddressType.IPV6:
            return Request(ipaddress.IPv6Address(address), number)
        case AddressType.DOMAIN:
            try:
                name = address[1:].decode("ascii")
            except UnicodeDecodeError:
                raise ProtocolError("a domain name that is not ASCII", Reply.HOST_UNREACHABLE) from None
            if not name:
                raise ProtocolError("an empty domain name", Reply.HOST_UNREACHABLE)
            return Request(name.rstrip("."), number)


def reply(code: Reply, bound: ipaddress.IPv4Address | ipaddress.IPv6Address | None = None, port: int = 0) -> bytes:
    """A reply. A failure names no bound address: 0.0.0.0:0."""
    address = bound or ipaddress.IPv4Address(0)
    kind = AddressType.IPV4 if address.version == 4 else AddressType.IPV6
    return bytes([VERSION, code, 0, kind]) + address.packed + port.to_bytes(2, "big")
