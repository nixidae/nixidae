#!/usr/bin/env python3
"""Two guests, one Ethernet segment.

Checks the things everything else is built on: that guests boot, that the
host can talk to each of them over the serial line, that frames get
between them on vec1, and that each uplink is the one vivarium promises,
10.0.2.15/24 via 10.0.2.2, in a build sandbox too: unpatched, passt gives a
sandboxed guest 169.254.2.1/16 instead.
"""

import asyncio

from vivarium_runner import Machines

UPLINK = "10.0.2.15/24"
GATEWAY = "10.0.2.2"


async def test(vms: Machines) -> None:
    server, client = vms.server, vms.client

    for vm in (server, client):
        await vm.succeed(f"test $(hostname) = {vm.name}")
        print(f"[test] {vm.name} vec1: {await vm.succeed('ip -4 -br addr show vec1')}")

    for vm in (server, client):
        addr = ""
        for _ in range(60):
            rc, addr = await vm.execute("ip -4 -br addr show vec0")
            if rc == 0 and UPLINK in addr:
                break
            await asyncio.sleep(1)
        else:
            raise AssertionError(f"{vm.name} vec0 is {addr.strip()!r}, not {UPLINK}")
        route = await vm.succeed("ip -4 route show default")
        assert f"via {GATEWAY} " in route, f"{vm.name}: default is {route.strip()!r}"
        print(f"[test] {vm.name} uplink: {UPLINK} via {GATEWAY}")

    await server.succeed(f"ping -c2 {client.ip}")
    await client.succeed(f"ping -c2 {server.ip}")
    print("[test] both directions ping")

    await server.wait_for_unit("sshd.service")
    failed = await server.execute("systemctl --failed --no-legend")
    print(f"[test] failed units on {server.name}: {failed[1] or 'none'}")
