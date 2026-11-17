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

import itertools
import os
import socket
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict

from . import sysctl

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


class PortMapJson(TypedDict):
    host: int | None
    guest: int


class RuleJson(TypedDict, total=False):
    """A `Rule` as Nix wrote it."""

    address: str
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
            address=data.get("address"),
            ports=tuple(PortMap.from_json(port) for port in data.get("ports", [])),
            protocols=tuple(data.get("protocols", ["tcp"])),
            remap_privileged=data.get("remapPrivileged", True),
            privileged_offset=data.get("privilegedOffset", 10000),
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


# ── egress arguments, for a guest behind the router ────────────────


def egress_args(rules: list[Rule], guest_address: str, router: str) -> list[str]:
    """The egress's FORWARD arguments for one guest: one host port each."""
    args: list[str] = []
    for rule in rules:
        if rule.address is None:
            raise ForwardError("egress_args() runs after resolve()")
        for port in rule.ports:
            if port.host is None:
                raise ForwardError("the egress gets a host port for every guest port; resolve() picks them")
            args += [
                f"{protocol}/{rule.address}:{port.host}={guest_address}:{port.guest}@{router}"
                for protocol in rule.protocols
            ]
    return args


def reachable(rules: list[Rule], guest_port: int) -> list[str]:
    """Where a guest port can be reached from the host, as ``addr:port``.

    Empty if nothing forwards it, which is the interesting answer: it is
    what tells you a service came up somewhere the host cannot see.
    """
    return [f"{rule.address}:{port.host}" for rule in rules for port in rule.ports if port.guest == guest_port]


# The guest's own address on its uplink, and the two addresses passt
# answers on behind it.
#
# passt shares the *host's* address by default: a guest is given the
# host's real IPv4, netmask and router, so the host's LAN is on-link to
# the guest and a test's environment depends on where the host is. On a
# hosted machine that LAN has other people's servers on it.
#
# 10.0.2.0/24 instead, which is the range QEMU's own user-mode network
# has used for twenty years, so it is the one a reader already knows --
# and the one modules/k8s.nix has described as the guest's uplink all
# along.
#
# Every guest gets the same address, and that is right: each has a passt
# of its own, none of them shares a link, and two guests that must reach
# each other do it on `vec1` with addresses a test chose. What makes a
# guest reachable from the host is its own 127.0.0.x, which forward.py
# hands out per guest and which is unaffected by any of this.
UPLINK_ADDRESS = "10.0.2.15"
UPLINK_PREFIX = 24
UPLINK_GATEWAY = "10.0.2.2"


# Where passt answers DNS for the guest.  Free in the uplink /24, and the
# address QEMU's own user-mode networking has used for this for years.
UPLINK_DNS = "10.0.2.3"

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


ROUTER_GATEWAY6 = "fd00:10:0:2::2"
"""The router's IPv6 gateway, which the egress maps to the host's ::1.
`prefix6` in modules/router.nix."""

OFFLINE_OUTBOUND6 = "::1"


def upstream_dns() -> str:
    """Where the uplink's resolver forwards: `VIVARIUM_DNS_HOST`, else 1.1.1.1."""
    return os.environ.get(ENV_DNS) or UPLINK_UPSTREAM_DNS


def uplink_notes(offline: bool = False) -> list[str]:
    """The console lines for a guest behind passt or pasta.

    passt's own banner is not in a QEMU run's output, and a guest that
    cannot resolve gives no clue which of the two addresses is wrong.
    """
    return [
        f"[uplink] DNS on {UPLINK_DNS}, forwarded to {upstream_dns()}",
        *(
            [f"[uplink] offline: outbound bound to {OFFLINE_OUTBOUND}, so the guest reaches nothing off this host"]
            if offline
            else []
        ),
    ]


def egress_notes(offline: bool = False) -> list[str]:
    """The console lines for a guest behind the egress: `uplink_notes` for a router."""
    return [
        f"[uplink] router: DNS on {UPLINK_DNS}, forwarded to {upstream_dns()}",
        *(
            [f"[uplink] router offline: outbound bound to {OFFLINE_OUTBOUND} and {OFFLINE_OUTBOUND6}"]
            if offline
            else []
        ),
    ]


def egress_uplink_args(offline: bool = False) -> list[str]:
    """The router's egress options that match `uplink_args`: DNS, the IPv6
    gateway, offline."""
    upstream = upstream_dns()
    return [
        f"--map={UPLINK_DNS}:53={upstream}:53",
        f"--loopback={ROUTER_GATEWAY6}",
        *([f"--outbound={OFFLINE_OUTBOUND}", f"--outbound={OFFLINE_OUTBOUND6}"] if offline else []),
    ]


def uplink_args(offline: bool = False) -> list[str]:
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
        UPLINK_ADDRESS,
        "--netmask",
        str(UPLINK_PREFIX),
        "--gateway",
        UPLINK_GATEWAY,
        "--dns-forward",
        UPLINK_DNS,
        "--dns-host",
        upstream,
        *(["--outbound", OFFLINE_OUTBOUND] if offline else []),
    ]
