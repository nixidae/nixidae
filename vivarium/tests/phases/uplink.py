#!/usr/bin/env python3
"""Can the guest reach anything off this host?

Prints the answer rather than asserting it, because the answer depends
on how the run was started. `--offline` must make it no; a plain run on
a connected host must make it yes; and a sandboxed run is no either way,
because the sandbox has no network for passt to use.

DNS and TCP separately: they fail differently, and a run that resolves
but cannot connect is a different fault from one that cannot resolve.
IPv6 separately from IPv4, and the uplink's own addresses and ICMP to its
gateway: an `--offline` run once let IPv6 out, carried the host's IPv6
prefix and answered pings, where a build did none of it (nixidae#1094).
"""

from vivarium_runner import Machines

TARGET = "1.1.1.1"
"""An address, not a name, so the TCP answer does not depend on DNS."""

TARGET6 = "2606:4700:4700::1111"


async def test(vms: Machines) -> None:
    rc, out = await vms.one.execute(
        f"timeout 5 bash -c '</dev/tcp/{TARGET}/53' 2>&1", timeout=20
    )
    print(f"[test] tcp to {TARGET}:53 -> {'reachable' if rc == 0 else 'no route'}")

    # SLAAC needs a router advertisement first; checked at once, a
    # connected run shows only link-local and looks as offline as it is not.
    await vms.one.execute("sleep 8", timeout=20)
    rc, out = await vms.one.execute(
        f"timeout 5 bash -c '</dev/tcp/{TARGET6}/53' 2>&1", timeout=20
    )
    print(f"[test] tcp to [{TARGET6}]:53 -> {'reachable' if rc == 0 else 'no route'}")

    rc, out = await vms.one.execute("ip -6 -br addr show vec0", timeout=20)
    print(f"[test] vec0 IPv6 -> {' '.join(out.split()[2:]) or 'none'}")

    rc, out = await vms.one.execute("ping -c 2 -W 2 10.0.2.2 >/dev/null 2>&1", timeout=20)
    print(f"[test] ping 10.0.2.2 -> {'answered' if rc == 0 else 'no answer'}")

    rc, out = await vms.one.execute(
        "timeout 5 getent hosts example.com 2>&1", timeout=20
    )
    print(f"[test] dns for example.com -> {'answered' if rc == 0 else 'no answer'}")
