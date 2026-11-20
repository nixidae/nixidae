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
"""

from __future__ import annotations

from vivarium_runner import Machines

TARGET = "1.1.1.1"
"""An address, not a name, so the TCP answer does not depend on DNS."""

TARGET6 = "2606:4700:4700::1111"

GATEWAY = "10.0.2.2"
"""`gateway` in nix/uplink.nix."""


async def test(vms: Machines) -> None:
    out_of_host: list[str] = []

    rc = (await vms.one.execute(f"timeout 5 bash -c '</dev/tcp/{TARGET}/53' 2>&1", timeout=20)).returncode
    print(f"[test] tcp to {TARGET}:53 -> {'reachable' if rc == 0 else 'no route'}")
    if rc == 0:
        out_of_host.append(f"tcp to {TARGET}:53")

    # SLAAC needs a router advertisement first; checked at once, a
    # connected run shows only link-local and looks as offline as it is not.
    await vms.one.execute("sleep 8", timeout=20)
    rc = (await vms.one.execute(f"timeout 5 bash -c '</dev/tcp/{TARGET6}/53' 2>&1", timeout=20)).returncode
    print(f"[test] tcp to [{TARGET6}]:53 -> {'reachable' if rc == 0 else 'no route'}")
    if rc == 0:
        out_of_host.append(f"tcp to [{TARGET6}]:53")

    addresses = (await vms.one.execute("ip -6 -br addr show vec0", timeout=20)).stdout.split()[2:]
    print(f"[test] vec0 IPv6 -> {' '.join(addresses) or 'none'}")
    out_of_host += [f"vec0 has {address}" for address in addresses if not address.startswith("fe80:")]

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
