#!/usr/bin/env python3
"""Two guests, one Ethernet segment.

Checks the things everything else is built on: that guests boot, that the
host can talk to each of them over the serial line, that frames get
between them on vec1, and that each uplink is the one vivarium promises,
`uplink_ip`/24 via 10.0.2.2, in a build sandbox too. `lan-router` runs
it with the guests behind a router, where each has its own address.
"""

from __future__ import annotations

import anyio
from vivarium_runner import GuestTimeout, Machines

GATEWAY = "10.0.2.2"


async def test(vms: Machines) -> None:
    server, client = vms.server, vms.client

    for vm in (server, client):
        await vm.succeed(f"test $(hostname) = {vm.name}")
        print(f"[test] {vm.name} vec1: {(await vm.succeed('ip -4 -br addr show vec1')).stdout}")

    for vm in (server, client):
        uplink = f"{vm.uplink_ip}/24"
        addr = ""
        for _ in range(60):
            shown = await vm.execute("ip -4 -br addr show vec0")
            addr = shown.stdout
            if shown.returncode == 0 and f" {uplink}" in addr:
                break
            await anyio.sleep(1)
        else:
            raise AssertionError(f"{vm.name} vec0 is {addr.strip()!r}, not {uplink}")
        route = (await vm.succeed("ip -4 route show default")).stdout
        assert f"via {GATEWAY} " in route, f"{vm.name}: default is {route.strip()!r}"
        print(f"[test] {vm.name} uplink: {uplink} via {GATEWAY}")

    await server.succeed(f"ping -c2 {client.ip}")
    await client.succeed(f"ping -c2 {server.ip}")
    print("[test] both directions ping")

    try:
        await server.execute("sleep 60 >/dev/null 2>&1 & echo $!; exec sleep 60 >/dev/null", timeout=1)
    except GuestTimeout as error:
        child = int(str(error).rpartition("\n")[2])
    else:
        raise AssertionError("a command past its timeout returned")
    if child in {p["pid"] for p in await server.processes()}:
        raise AssertionError(f"pid {child} outlived the command that timed out")
    print("[test] a command past its timeout raises GuestTimeout, and its child is gone")

    await server.wait_for_unit("sshd.service")
    failed = (await server.execute("systemctl --failed --no-legend")).stdout
    print(f"[test] failed units on {server.name}: {failed or 'none'}")
