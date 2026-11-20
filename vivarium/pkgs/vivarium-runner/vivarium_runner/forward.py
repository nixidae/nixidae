"""Turning `vivarium.inbound` into host sockets and egress arguments.

An inbound entry is a guest port, and a host port or none: none takes
any free one. The runner binds a forward through an egress itself and
hands the socket over (nixidae/nixidae#1546).

Every guest's forwards share one host address, `LOOPBACK`: the bind is
the claim, and a taken fixed port fails the boot by name.
"""

from __future__ import annotations

import errno
import ipaddress
import os
import socket
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, TypedDict

from .spec import closed

_UNPRIVILEGED_START = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")

_DEFAULT_UNPRIVILEGED_START = 1024

LOOPBACK: Final = "127.0.0.1"
"""Where a forward with no address listens, for every guest of every run.
One address, so two runs that want one fixed port meet: the first bind
keeps it (nixidae/nixidae#1546). macOS has no other on ``lo0``."""


class ForwardError(Exception):
    """A forward cannot be set up: a collision, or nowhere to put it."""


@closed
class InboundJson(TypedDict):
    """One `vivarium.inbound` entry as Nix wrote it."""

    guest: int
    host: int | None
    protocols: list[str]
    address: str | None


@dataclass(frozen=True)
class PortMap:
    """One host port and the guest port it reaches."""

    host: int | None
    """`None` until the runner binds or picks any free one."""
    guest: int

    def __str__(self) -> str:
        return f"{self.host}" if self.host == self.guest else f"{self.host}:{self.guest}"


@dataclass(frozen=True)
class Rule:
    """One ``(address, ports)`` pair to bind on the host.

    ``address`` is None until :func:`resolve` picks one.
    """

    address: str | None = None
    ports: tuple[PortMap, ...] = ()
    protocols: tuple[str, ...] = ("tcp",)

    @classmethod
    def from_json(cls, data: InboundJson) -> Rule:
        return cls(
            address=data["address"],
            ports=(PortMap(host=data["host"], guest=data["guest"]),),
            protocols=tuple(data["protocols"]),
        )

    def host_ports(self) -> list[int]:
        """The fixed host ports of this rule."""
        return [port.host for port in self.ports if port.host is not None]


# ── the host's own limits ──────────────────────────────────────────


def _read_ints(path: Path, fallback: tuple[int, ...]) -> tuple[int, ...]:
    try:
        return tuple(int(field) for field in path.read_text().split())
    except (OSError, ValueError):
        return fallback


def unprivileged_start() -> int:
    """Lowest port the host will let an unprivileged process bind.

    1024 unless someone has lowered ``net.ipv4.ip_unprivileged_port_start``,
    which is the whole reason guest port 22 is not simply host port 22.
    """
    return _read_ints(_UNPRIVILEGED_START, (_DEFAULT_UNPRIVILEGED_START,))[0]


def _bind(address: str, port: int) -> socket.socket:
    """Bind one port the way a listener will, so a probe means what it says."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Listeners set this (Go's net.Listen too), so probing without it
    # would call a TIME_WAIT leftover a collision.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((address, port))
    except OSError:
        sock.close()
        raise
    return sock


# ── claims: the runner binds what the egress serves ────────────────


@dataclass(frozen=True)
class Claim:
    """A host socket the runner bound for one guest port.

    The bind is the claim: nothing else takes the port between a choice
    and the egress's start (nixidae/nixidae#1546). The runner keeps the
    socket for the guest's life and gives each egress it starts a copy.
    """

    protocol: str
    sock: socket.socket
    guest: int


_RANDOM_TRIES: Final = 32
"""Binds of one random port before giving up. A port the kernel picked
for one protocol can be taken for another; each try is a new pick."""


def _listen(protocol: str, address: str, port: int) -> socket.socket:
    """*protocol* bound on *address*:*port*; a TCP one listens."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM if protocol == "tcp" else socket.SOCK_DGRAM)
    try:
        if protocol == "tcp":
            # As the egress's own listeners: a TIME_WAIT leftover is no
            # collision. A port another socket listens on still is.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((address, port))
        if protocol == "tcp":
            sock.listen(socket.SOMAXCONN)
    except BaseException:
        sock.close()
        raise
    return sock


def _claim_port(address: str, protocols: tuple[str, ...], port: int | None) -> tuple[int, list[Claim]]:
    """One host port on *address* for each of *protocols*, the same number
    for all: *port*, or any free one when it is None."""
    for _ in range(_RANDOM_TRIES if port is None else 1):
        claims: list[Claim] = []
        try:
            chosen = port or 0
            for protocol in protocols:
                sock = _listen(protocol, address, chosen)
                chosen = sock.getsockname()[1]
                claims.append(Claim(protocol, sock, guest=0))
            return chosen, claims
        except OSError as error:
            for held in claims:
                held.sock.close()
            if port is not None:
                if error.errno == errno.EADDRINUSE:
                    raise ForwardError(
                        f"cannot forward port {port} on {address}: {address}:{port} is already bound"
                    ) from error
                raise ForwardError(f"cannot forward port {port} on {address}: {error}") from error
            if error.errno != errno.EADDRINUSE:
                raise ForwardError(f"cannot forward a port on {address}: {error}") from error
    raise ForwardError(f"no host port on {address} free for {', '.join(protocols)} after {_RANDOM_TRIES} tries")


def claim(rules: list[Rule], *, random: bool) -> tuple[list[Rule], list[Claim]]:
    """Bind every port of *rules*, after :func:`resolve`: the rules as
    bound, and the sockets. *random* binds any free port in place of the
    one each rule names: a build's choice, where nobody outside reaches a
    port and macOS builds share one loopback."""
    bound: list[Rule] = []
    claims: list[Claim] = []
    try:
        for rule in rules:
            if rule.address is None:
                raise ForwardError(f"forward of {rule.ports} has no address: claim() runs after resolve()")
            ports: list[PortMap] = []
            for port in rule.ports:
                host, bound_now = _claim_port(rule.address, rule.protocols, None if random else port.host)
                claims += [replace(one, guest=port.guest) for one in bound_now]
                ports.append(replace(port, host=host))
            bound.append(replace(rule, ports=tuple(ports)))
    except BaseException:
        for held in claims:
            held.sock.close()
        raise
    return bound, claims


def resolve(rules: list[Rule], *, start: int | None = None) -> list[Rule]:
    """Give every rule a concrete address, `LOOPBACK` where it named none;
    refuse a privileged host port."""
    if start is None:
        start = unprivileged_start()
    for rule in rules:
        for port in rule.ports:
            if port.host is not None and port.host < start:
                raise ForwardError(
                    f"host port {port.host} for guest port {port.guest} needs privileges: "
                    f"ask for {start} or above, or for none to take any free port"
                )

    return [replace(rule, address=LOOPBACK) if rule.address is None else rule for rule in rules]


# ── what the egress takes, for a guest behind it ───────────────────


@dataclass(frozen=True)
class Add:
    """One claimed socket for the egress, and where its flows go."""

    claim: Claim
    guest_address: str
    router: str

    def line(self) -> bytes:
        """The control message the socket travels with (`egress.rs`, `parse_add`)."""
        return f"add {self.claim.protocol} {self.guest_address}:{self.claim.guest} {self.router}\n".encode()

    def fds(self) -> list[int]:
        return [self.claim.sock.fileno()]


@dataclass(frozen=True)
class Remove:
    """The egress stops serving one claimed socket, and ends its flows."""

    claim: Claim

    def line(self) -> bytes:
        """The control message (`egress.rs`, `parse_message`)."""
        address, port = self.claim.sock.getsockname()[:2]
        return f"remove {self.claim.protocol} {address}:{port}\n".encode()

    def fds(self) -> list[int]:
        return []


def egress_adds(claims: list[Claim], guest_address: str, router: str) -> list[Add]:
    """What the egress takes for one guest: each claimed socket, reaching
    its guest port at *guest_address* through *router*."""
    return [Add(one, guest_address, router) for one in claims]


def diff(old: list[Rule], new: list[Rule]) -> tuple[list[Rule], list[Rule]]:
    """The entries of *old* that *new* drops, and those *new* adds: what a
    switch to *new* changes. An entry with a changed host port, address or
    protocol is one of each."""
    gone, added = list(old), []
    for rule in new:
        if rule in gone:
            gone.remove(rule)
        else:
            added.append(rule)
    return gone, added


def reachable(rules: list[Rule], guest_port: int, protocol: str = "tcp") -> list[str]:
    """Where a guest port can be reached from the host over *protocol*,
    as ``addr:port``.

    Empty if nothing forwards it, which is the interesting answer: it is
    what tells you a service came up somewhere the host cannot see.
    """
    return [
        f"{rule.address}:{port.host}"
        for rule in rules
        if protocol in rule.protocols
        for port in rule.ports
        if port.guest == guest_port
    ]


@closed
class UplinkNetworkJson(TypedDict):
    """`UplinkNetwork` as Nix wrote it: `spec` in nix/uplink.nix."""

    address: str
    prefixLength: int  # noqa: N815 -- Nix writes it, so Nix spells it
    gateway: str
    dns: str
    gateway6: str


@dataclass(frozen=True)
class UplinkNetwork:
    """The guest's own address on its uplink, and what answers behind it.

    nix/uplink.nix writes them, for the relay and the router alike.
    Every guest gets the same address: each has an uplink of its own, and
    the host reaches a guest through its own 127.0.0.x.
    """

    address: str
    prefix_length: int
    gateway: str
    dns: str
    """Where the egress answers DNS for the guest."""
    gateway6: str
    """The router's IPv6 gateway, which the egress maps to the host's ::1."""

    @classmethod
    def from_json(cls, data: UplinkNetworkJson) -> UplinkNetwork:
        return cls(
            address=data["address"],
            prefix_length=data["prefixLength"],
            gateway=data["gateway"],
            dns=data["dns"],
            gateway6=data["gateway6"],
        )

    @property
    def subnet(self) -> str:
        return str(ipaddress.ip_interface(f"{self.address}/{self.prefix_length}").network)


# Where the egress sends a query it is asked to forward.  A constant, and not
# the host's own resolver: reading the host's nameserver files gave a
# different answer on a laptop and on a GitHub runner, and the runner's
# answer was the 127.0.0.53 stub, which no guest can reach.  Two rounds of
# CI went on deriving this address before the derivation itself was the
# thing to remove.
#
# The guests do not depend on it.  modules/guest.nix names its own
# resolvers and drops the ones DHCP offers, so this covers only something
# that asks the uplink's resolver directly.
UPLINK_UPSTREAM_DNS = "1.1.1.1"

# For a network that does not reach 1.1.1.1.  The one knob, and the only
# thing here that looks at the host at all.
ENV_DNS = "VIVARIUM_DNS_HOST"


OFFLINE_OUTBOUND = "127.0.0.1"
"""What the egress binds its outbound sockets to when a run asks for no
internet.

Loopback, so a connection the guest makes to anything off the host has no
route and fails at once.  Everything else about the uplink is unchanged
-- the guest still has its address, its gateway and its resolver, and the
host's way *in* still works -- because the guest must look the same as it
does in a sandbox, where the egress runs and finds no usable route either.

Not simply leaving the egress out: that takes the uplink away, so the
guest has no inbound forwards.  A test that reaches an API server through
a forward would then fail for a reason that is not the one being
reproduced."""


OFFLINE_OUTBOUND6 = "::1"


def upstream_dns() -> str:
    """Where the uplink's resolver forwards: `VIVARIUM_DNS_HOST`, else 1.1.1.1."""
    return os.environ.get(ENV_DNS) or UPLINK_UPSTREAM_DNS


def egress_notes(network: UplinkNetwork, offline: bool = False) -> list[str]:
    """The console lines for a guest behind the egress.

    A guest that cannot resolve gives no clue which of the two addresses
    is wrong.
    """
    return [
        f"[uplink] router: DNS on {network.dns}, forwarded to {upstream_dns()}",
        *(
            [f"[uplink] router offline: outbound bound to {OFFLINE_OUTBOUND} and {OFFLINE_OUTBOUND6}"]
            if offline
            else []
        ),
    ]


def egress_uplink_args(network: UplinkNetwork, offline: bool = False) -> list[str]:
    """The egress options for the uplink: DNS, the IPv6 gateway, offline."""
    upstream = upstream_dns()
    return [
        f"--map={network.dns}:53={upstream}:53",
        f"--loopback={network.gateway6}",
        *([f"--outbound={OFFLINE_OUTBOUND}", f"--outbound={OFFLINE_OUTBOUND6}"] if offline else []),
    ]
