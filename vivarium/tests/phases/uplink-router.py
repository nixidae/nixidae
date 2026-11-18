"""A guest behind the router has the address the router leases it, and reaches the host through the relay.

The guest need not be NixOS: the router's DHCP server leases by MAC, and
the relay carries whatever reaches the router.
"""

from __future__ import annotations

import socket

import anyio
from vivarium_runner import Machine, Machines

GATEWAY = "10.0.2.2"
"""The uplink's gateway: the egress maps it to the host's loopback (nix/uplink.nix)."""


async def answer(server: socket.socket) -> None:
    await anyio.wait_readable(server)
    conn, _ = server.accept()
    with conn:
        conn.sendall(b"host\n")


async def reach(vm: Machine) -> None:
    addresses = (await vm.succeed("ip -4 -o addr show")).stdout
    uplink = [line.split()[3] for line in addresses.splitlines() if " 10.0.2." in line]
    if not uplink:
        raise AssertionError(f"{vm.name} has no address on the uplink's subnet:\n{addresses}")
    print(f"[test] {vm.name} leased {', '.join(uplink)} from the router")

    # A server on the host's loopback, which the guest reaches as the gateway.
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.setblocking(False)
        port = server.getsockname()[1]
        async with anyio.create_task_group() as group:
            group.start_soon(answer, server)
            command = f"timeout 10 bash -c 'exec 3<>/dev/tcp/{GATEWAY}/{port}; cat <&3'"
            got = (await vm.succeed(command)).stdout.strip()
    if got != "host":
        raise AssertionError(f"{vm.name}: the host's server said {got!r}")
    print(f"[test] {vm.name} reached the host's loopback through the router")


async def test(vms: Machines) -> None:
    for name, vm in vms.items():
        if name != "router":
            await reach(vm)
