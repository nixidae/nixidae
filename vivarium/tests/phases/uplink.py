#!/usr/bin/env python3
"""Does nothing leave the host?

The answer `--offline` and a build must give. A plain run on a connected
host gives the other answer, so it fails this phase, and the failure lists
what got out: that run is the negative control.

DNS and TCP separately: they fail differently, and a run that resolves
but cannot connect is a different fault from one that cannot resolve.
IPv6 separately from IPv4, and the uplink's own addresses: an `--offline`
run once let IPv6 out and carried the host's IPv6 prefix, where a build
did neither (nixidae#1094).

A finished handshake proves nothing: the guest's relay accepts every TCP
connection itself and only then asks the egress for the far end. So TCP
counts as out only when the far end answers a DNS query over it.
"""

from __future__ import annotations

import ipaddress

from vivarium_runner import Machines

TARGET = "1.1.1.1"
"""An address, not a name, so the TCP answer does not depend on DNS."""

TARGET6 = "2606:4700:4700::1111"

GATEWAY = "10.0.2.2"
"""`gateway` in nix/uplink.nix."""

QUERY = r"\x00\x11\x12\x34\x01\x00\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x01"
"""A DNS query for the root's NS records, with the 2-byte length TCP needs."""


def answers(target: str) -> str:
    """A shell line that exits 0 when *target* answers `QUERY` on port 53."""
    return (
        f'timeout 5 bash -c \'exec 3<>/dev/tcp/{target}/53 && printf "{QUERY}" >&3'
        ' && test "$(head -c 2 <&3 | wc -c)" = 2\' 2>&1'
    )


async def test(vms: Machines) -> None:
    out_of_host: list[str] = []

    rc = (await vms.one.execute(answers(TARGET), timeout=20)).returncode
    print(f"[test] tcp to {TARGET}:53 -> {'reachable' if rc == 0 else 'no route'}")
    if rc == 0:
        out_of_host.append(f"tcp to {TARGET}:53")

    # SLAAC needs a router advertisement first; checked at once, a
    # connected run shows only link-local and looks as offline as it is not.
    await vms.one.execute("sleep 8", timeout=20)
    rc = (await vms.one.execute(answers(TARGET6), timeout=20)).returncode
    print(f"[test] tcp to [{TARGET6}]:53 -> {'reachable' if rc == 0 else 'no route'}")
    if rc == 0:
        out_of_host.append(f"tcp to [{TARGET6}]:53")

    addresses = (await vms.one.execute("ip -6 -br addr show vec0", timeout=20)).stdout.split()[2:]
    print(f"[test] vec0 IPv6 -> {' '.join(addresses) or 'none'}")
    # The uplink's own ULA is the run's; any other global address came
    # from the host's network.
    own = ipaddress.ip_network(f"{vms.one.spec.uplink_network.gateway6}/64", strict=False)
    out_of_host += [
        f"vec0 has {address}"
        for address in addresses
        if not (interface := ipaddress.ip_interface(address)).is_link_local and interface.ip not in own
    ]

    # Not past the host: the egress answers it, offline too
    # (`namespace.allow_ping`).
    rc = (await vms.one.execute(f"ping -c 2 -W 2 {GATEWAY} >/dev/null 2>&1", timeout=20)).returncode
    print(f"[test] ping {GATEWAY} -> {'answered' if rc == 0 else 'no answer'}")

    rc = (await vms.one.execute("timeout 5 getent hosts example.com 2>&1", timeout=20)).returncode
    print(f"[test] dns for example.com -> {'answered' if rc == 0 else 'no answer'}")
    if rc == 0:
        out_of_host.append("dns for example.com")

    if out_of_host:
        raise AssertionError(f"the guest reached past the host: {', '.join(out_of_host)}")
