"""Turning port-forward rules into passt and egress arguments.

A forward is a listed port. passt binds every forward while it parses
its options and takes no new one later, so the forwards are decided
before the guest boots; the egress takes them the same way.

Each guest gets a loopback address of its own on Linux, so two guests
never collide on a port. macOS has 127.0.0.1 alone: there the guests
share it, and :func:`resolve` moves a port that is taken.

:func:`probe` exists because passt reports a port that will not bind
as a bare ``Address already in use`` and exits, naming neither the
guest nor the rule.
"""

from __future__ import annotations

import errno
import ipaddress
import itertools
import os
import socket
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, TypedDict

from . import sysctl
from .spec import closed

_UNPRIVILEGED_START = Path("/proc/sys/net/ipv4/ip_unprivileged_port_start")
_LOCAL_PORT_RANGE = Path("/proc/sys/net/ipv4/ip_local_port_range")

_DEFAULT_UNPRIVILEGED_START = 1024
_DEFAULT_EPHEMERAL = (32768, 60999)
"""What passt's own ``fwd_probe_ephemeral()`` falls back to (RFC 6335)."""

_LOOPBACK_FIRST = 2
_LOOPBACK_LAST = 254
"""``127.0.0.2`` through ``127.0.0.254``.  All of ``127.0.0.0/8`` is on
``lo`` without anyone having to configure it, so a guest can be given an
address of its own with no privileges and no setup -- and 127.0.0.1 is
left alone, because the host's own services are there."""


class ForwardError(Exception):
    """A forward cannot be set up: a collision, or nowhere to put it."""


@closed
class PortMapJson(TypedDict):
    host: int | None
    guest: int


@closed
class RuleJson(TypedDict):
    """A `Rule` as Nix wrote it."""

    address: str | None
    ports: list[PortMapJson | int]
    protocols: list[str]
    remapPrivileged: bool
    privilegedOffset: int


@dataclass(frozen=True)
class PortMap:
    """One host port and the guest port it reaches."""

    host: int | None
    """`None` until :func:`resolve` picks a free one: the guest's own
    port, or the first free one above it."""
    guest: int

    @classmethod
    def from_json(cls, data: PortMapJson | int) -> PortMap:
        if isinstance(data, int):
            return cls(host=data, guest=data)
        return cls(host=data["host"], guest=data["guest"])

    def __str__(self) -> str:
        return f"{self.host}" if self.host == self.guest else f"{self.host}:{self.guest}"


@dataclass(frozen=True)
class Rule:
    """One ``(address, ports)`` pair to hand passt.

    ``address`` is None until :func:`resolve` picks one.
    """

    address: str | None = None
    ports: tuple[PortMap, ...] = ()
    protocols: tuple[str, ...] = ("tcp",)
    remap_privileged: bool = True
    privileged_offset: int = 10000

    @classmethod
    def from_json(cls, data: RuleJson) -> Rule:
        return cls(
            address=data["address"],
            ports=tuple(PortMap.from_json(port) for port in data["ports"]),
            protocols=tuple(data["protocols"]),
            remap_privileged=data["remapPrivileged"],
            privileged_offset=data["privilegedOffset"],
        )

    def host_ports(self) -> list[int]:
        """Host ports this rule needs bound, for probing."""
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


def ephemeral_range() -> tuple[int, int]:
    """The host's ephemeral port range, as passt probes it."""
    if sys.platform == "darwin":
        return sysctl.read("net.inet.ip.portrange.first"), sysctl.read("net.inet.ip.portrange.last")
    values = _read_ints(_LOCAL_PORT_RANGE, _DEFAULT_EPHEMERAL)
    return (values[0], values[1]) if len(values) == 2 else _DEFAULT_EPHEMERAL


# ── resolution ─────────────────────────────────────────────────────


def _remap(rule: Rule, start: int) -> tuple[Rule, list[str]]:
    """Move host ports the kernel will not give us out of the way.

    Returns the rule and whatever the user should be told about it.  A
    remap is never silent: the port you asked for is not the port you
    get, and finding that out from a refused connection is worse than
    reading it at boot.
    """
    if start <= 1:
        return rule, []

    offset = rule.privileged_offset
    kept: list[PortMap] = []
    notes: list[str] = []
    for port in rule.ports:
        if port.host is None or port.host >= start:
            kept.append(port)
        elif rule.remap_privileged:
            moved = replace(port, host=port.host + offset)
            kept.append(moved)
            notes.append(
                f"host port {port.host} needs privileges we do not have; "
                f"forwarding {moved.host} -> guest {port.guest} instead"
            )
        else:
            notes.append(
                f"host port {port.host} needs privileges we do not have "
                f"and remapPrivileged is off; guest {port.guest} is unreachable"
            )
    return replace(rule, ports=tuple(kept)), notes


def _bind(address: str, port: int) -> socket.socket:
    """Bind one port the way passt will, so a probe means what it says."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # passt sets this on every listening socket (util.c:109), so probing
    # without it would call a TIME_WAIT leftover a collision.
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
                if port.host is None:
                    raise ForwardError("claim() gets a host port for every guest port; resolve() picks them")
                host, bound_now = _claim_port(rule.address, rule.protocols, None if random else port.host)
                claims += [replace(one, guest=port.guest) for one in bound_now]
                ports.append(replace(port, host=host))
            bound.append(replace(rule, ports=tuple(ports)))
    except BaseException:
        for held in claims:
            held.sock.close()
        raise
    return bound, claims


def _probe(address: str, ports: list[int]) -> int | None:
    """The first of *ports* that will not bind on *address*, if any."""
    held: list[socket.socket] = []
    try:
        for port in ports:
            try:
                held.append(_bind(address, port))
            except OSError:
                return port
        return None
    finally:
        for sock in held:
            sock.close()


def _assign(rules: list[Rule], taken: set[str]) -> str:
    """Find a loopback address free for every rule that wants one."""
    wanted = sorted({port for rule in rules for port in rule.host_ports()})
    for octet in range(_LOOPBACK_FIRST, _LOOPBACK_LAST + 1):
        address = f"127.0.0.{octet}"
        if address in taken:
            continue
        if _probe(address, wanted) is None:
            return address
    raise ForwardError(
        f"no free address in 127.0.0.{_LOOPBACK_FIRST}-{_LOOPBACK_LAST} "
        f"for ports {', '.join(str(port) for port in wanted)}"
    )


def resolve(rules: list[Rule], *, taken: set[str], start: int | None = None) -> tuple[list[Rule], list[str]]:
    """Give every rule a concrete address and a bindable set of ports.

    *taken* is the addresses already handed to other guests in this run;
    it is updated in place.  Callers must resolve every guest before
    booting any of them, or two guests race for the same address.
    """
    if start is None:
        start = unprivileged_start()

    # Remap first: the address is chosen by binding the host ports, and a
    # privileged one binds nowhere, so every address would be refused.
    remapped = [_remap(rule, start) for rule in rules]
    rules = [rule for rule, _ in remapped]

    unaddressed = [rule for rule in rules if rule.address is None]
    if unaddressed and sys.platform == "darwin":
        rules = [_share(rule, taken) if rule.address is None else rule for rule in rules]
    elif unaddressed:
        address = _assign(unaddressed, taken)
        taken.add(address)
        rules = [replace(rule, address=address) if rule.address is None else rule for rule in rules]

    shared = f"{SHARED_LOOPBACK}:"
    reserved = {int(entry.removeprefix(shared)) for entry in taken if entry.removeprefix(shared).isdigit()}
    resolved: list[Rule] = []
    notes: list[str] = []
    for rule, (_, said) in zip(rules, remapped, strict=False):
        notes.extend(f"{rule.address}: {note}" for note in said)
        rule, picked = _pick_ports(rule, start, reserved if rule.address == SHARED_LOOPBACK else set())  # noqa: PLW2901 -- the same rule, with its ports settled
        notes.extend(f"{rule.address}: {note}" for note in picked)
        if rule.address == SHARED_LOOPBACK:
            for port in rule.host_ports():
                taken.add(f"{shared}{port}")
                reserved.add(port)
        if rule.ports:
            resolved.append(rule)
    return resolved, notes


SHARED_LOOPBACK = "127.0.0.1"
"""Every guest's forwards on macOS, where lo0 has 127.0.0.1 alone and an
alias needs root. The guests then share one port space: `resolve` moves a
port another guest or the host holds."""


def _share(rule: Rule, taken: set[str]) -> Rule:
    """*rule* on :data:`SHARED_LOOPBACK`, a host port that is not free there
    left for `_pick_ports` to move. *taken* holds `127.0.0.1:<port>` for each
    port another guest of the run has."""
    ports = tuple(
        replace(port, host=None)
        if port.host is not None
        and (f"{SHARED_LOOPBACK}:{port.host}" in taken or _probe(SHARED_LOOPBACK, [port.host]) is not None)
        else port
        for port in rule.ports
    )
    return replace(rule, address=SHARED_LOOPBACK, ports=ports)


def _pick_ports(rule: Rule, start: int, reserved: set[int] | None = None) -> tuple[Rule, list[str]]:
    """A free host port for each port map that names none: the guest's port,
    else the first free one above it, never in the ephemeral range nor in
    *reserved*, the ports other guests on the same address have."""
    if all(port.host is not None for port in rule.ports):
        return rule, []
    if rule.address is None:
        raise ForwardError("ports are picked after the address")
    low, high = ephemeral_range()
    used = set(rule.host_ports()) | (reserved or set())
    ports: list[PortMap] = []
    notes: list[str] = []
    for port in rule.ports:
        if port.host is not None:
            ports.append(port)
            continue
        first = port.guest if port.guest >= start else port.guest + rule.privileged_offset
        # Then below it: macOS's ephemeral range runs to 65535.
        host = next(
            (
                candidate
                for candidate in itertools.chain(range(first, 65536), range(first - 1, start - 1, -1))
                if not low <= candidate <= high and candidate not in used and _probe(rule.address, [candidate]) is None
            ),
            None,
        )
        if host is None:
            raise ForwardError(f"no free host port on {rule.address} from {first} for guest port {port.guest}")
        used.add(host)
        ports.append(replace(port, host=host))
        if host != first:
            notes.append(f"guest port {port.guest} on host port {host}: {first} is taken")
    return replace(rule, ports=tuple(ports)), notes


def probe(rules: list[Rule]) -> None:
    """Raise if anything a rule asks for is already bound.

    passt would find this out for itself and exit with a bare ``Address
    already in use``, naming neither the guest nor the rule.
    """
    for rule in rules:
        if rule.address is None:
            raise ForwardError(f"forward of {rule.ports} has no address: probe() runs after resolve()")
        clash = _probe(rule.address, rule.host_ports())
        if clash is not None:
            raise ForwardError(
                f"cannot forward port {clash} on {rule.address}: {rule.address}:{clash} is already bound"
            )


# ── passt arguments ────────────────────────────────────────────────


def _spec(rule: Rule) -> str:
    """The passt port spec a rule turns into."""
    if any(port.host is None for port in rule.ports):
        raise ForwardError("passt gets a host port for every guest port; resolve() picks them")
    return f"{rule.address}/" + ",".join(str(port) for port in sorted(rule.ports, key=lambda p: p.host or 0))


def to_args(rules: list[Rule]) -> list[str]:
    """Bridge arguments carrying *rules*, as ``--tcp-ports``/``--udp-ports``."""
    flags = {"tcp": "--tcp-ports", "udp": "--udp-ports"}
    return [arg for rule in rules for protocol in rule.protocols for arg in (flags[protocol], _spec(rule))]


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


def egress_adds(claims: list[Claim], guest_address: str, router: str) -> list[Add]:
    """What the egress takes for one guest: each claimed socket, reaching
    its guest port at *guest_address* through *router*."""
    return [Add(one, guest_address, router) for one in claims]


def reachable(rules: list[Rule], guest_port: int) -> list[str]:
    """Where a guest port can be reached from the host, as ``addr:port``.

    Empty if nothing forwards it, which is the interesting answer: it is
    what tells you a service came up somewhere the host cannot see.
    """
    return [f"{rule.address}:{port.host}" for rule in rules for port in rule.ports if port.guest == guest_port]


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

    nix/uplink.nix writes them, for passt, gvproxy and the router alike.
    Every guest gets the same address: each has an uplink of its own, and
    the host reaches a guest through its own 127.0.0.x.
    """

    address: str
    prefix_length: int
    gateway: str
    dns: str
    """Where passt and the egress answer DNS for the guest."""
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


# Where passt sends a query it is asked to forward.  A constant, and not
# the host's own resolver: reading the host's nameserver files gave a
# different answer on a laptop and on a GitHub runner, and the runner's
# answer was the 127.0.0.53 stub, which no guest can reach.  Two rounds of
# CI went on deriving this address before the derivation itself was the
# thing to remove.
#
# The guests do not depend on it.  modules/guest.nix names its own
# resolvers and drops the ones DHCP offers, so this covers only something
# that asks passt directly.
UPLINK_UPSTREAM_DNS = "1.1.1.1"

# For a network that does not reach 1.1.1.1.  The one knob, and the only
# thing here that looks at the host at all.
ENV_DNS = "VIVARIUM_DNS_HOST"


OFFLINE_OUTBOUND = "127.0.0.1"
"""What passt binds its outbound sockets to when a run asks for no
internet.

Loopback, so a connection the guest makes to anything off the host has no
route and fails at once.  Everything else about the uplink is unchanged
-- the guest still gets its address by DHCP, still hears about a gateway
and a resolver, and the host's way *in* still works -- because the guest
must look the same as it does in a sandbox, where passt runs and finds no
usable route either.

Not simply leaving passt out: that takes vec0 away, so the guest has no
uplink interface at all and no inbound forwards.  A test that reaches an
API server through a forward would then fail for a reason that is not the
one being reproduced."""


OFFLINE_OUTBOUND6 = "::1"


def upstream_dns() -> str:
    """Where the uplink's resolver forwards: `VIVARIUM_DNS_HOST`, else 1.1.1.1."""
    return os.environ.get(ENV_DNS) or UPLINK_UPSTREAM_DNS


def uplink_notes(network: UplinkNetwork, offline: bool = False) -> list[str]:
    """The console lines for a guest behind passt or pasta.

    passt's own banner is not in a QEMU run's output, and a guest that
    cannot resolve gives no clue which of the two addresses is wrong.
    """
    return [
        f"[uplink] DNS on {network.dns}, forwarded to {upstream_dns()}",
        *(
            [f"[uplink] offline: outbound bound to {OFFLINE_OUTBOUND}, so the guest reaches nothing off this host"]
            if offline
            else []
        ),
    ]


def egress_notes(network: UplinkNetwork, offline: bool = False) -> list[str]:
    """The console lines for a guest behind the egress: `uplink_notes` for a router."""
    return [
        f"[uplink] router: DNS on {network.dns}, forwarded to {upstream_dns()}",
        *(
            [f"[uplink] router offline: outbound bound to {OFFLINE_OUTBOUND} and {OFFLINE_OUTBOUND6}"]
            if offline
            else []
        ),
    ]


def egress_uplink_args(network: UplinkNetwork, offline: bool = False) -> list[str]:
    """The router's egress options that match `uplink_args`: DNS, the IPv6
    gateway, offline."""
    upstream = upstream_dns()
    return [
        f"--map={network.dns}:53={upstream}:53",
        f"--loopback={network.gateway6}",
        *([f"--outbound={OFFLINE_OUTBOUND}", f"--outbound={OFFLINE_OUTBOUND6}"] if offline else []),
    ]


def uplink_args(network: UplinkNetwork, offline: bool = False) -> list[str]:
    """passt arguments giving the guest an address of its own, and DNS.

    Both backends pass these -- QEMU straight to passt, UML through
    `uml-passt-bridge`'s `--passt` -- so that a guest's uplink looks the
    same whichever machine it turned out to be.

    `--dns-host` is named rather than left to passt's default, which is
    the first nameserver in the host's `/etc/resolv.conf`.  On a
    systemd-resolved host that is `127.0.0.53`, and passt then advertises
    it to the guest, where it means the guest's own resolver.  That is how
    every guest CI job came to fail on a runner with `lookup
    registry.k8s.io: no such host`.
    """
    upstream = upstream_dns()
    return [
        "--address",
        network.address,
        "--netmask",
        str(network.prefix_length),
        "--gateway",
        network.gateway,
        "--dns-forward",
        network.dns,
        "--dns-host",
        upstream,
        *(["--outbound", OFFLINE_OUTBOUND] if offline else []),
    ]
