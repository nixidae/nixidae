"""A guest behind the router whose uplink is eth0 (VyOS, SONiC): eth0 leases
the router's address, and the guest reaches the host through the relay."""

from __future__ import annotations

import ipaddress

import anyio
from anyio.abc import SocketAttribute, SocketStream
from vivarium_runner import Machine, MachineError, Machines

GATEWAY = "10.0.2.2"
"""The uplink's gateway: the egress maps it to the host's loopback (nix/uplink.nix)."""

LEASE = r"ip -4 -brief addr show eth0 | grep -m1 -oE '10\.0\.2\.[0-9]+'"

BODY = b"host"


async def serve(stream: SocketStream) -> None:
    async with stream:
        request = b""
        while b"\r\n\r\n" not in request:
            request += await stream.receive()
        await stream.send(b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(BODY), BODY))


async def lease(vm: Machine) -> ipaddress.IPv4Address:
    """eth0's address from the router. The DHCP client starts well after
    the agent is up, so show eth0 on a timeout."""
    try:
        text = (await vm.wait_until_succeeds(LEASE, timeout=300)).stdout.strip()
    except MachineError as error:
        shown = (await vm.execute("ip addr show eth0 2>&1")).stdout
        raise AssertionError(f"{error}\neth0:\n{shown}") from None
    return ipaddress.IPv4Address(text)


async def test(vms: Machines) -> None:
    (vm,) = (vm for name, vm in vms.items() if name != "router")
    address = await lease(vm)
    if str(address) != vm.uplink_ip:
        raise AssertionError(f"{vm.name} leased {address}, and the router gives it {vm.uplink_ip}")
    print(f"[test] {vm.name} leased {address} on eth0 from the router")

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    port = listener.extra(SocketAttribute.local_port)

    async def accept() -> None:
        await listener.serve(serve)

    async with listener, anyio.create_task_group() as group:
        group.start_soon(accept)
        got = (await vm.succeed(f"curl -sS --max-time 10 http://{GATEWAY}:{port}/")).stdout.strip()
        group.cancel_scope.cancel()
    if got != BODY.decode():
        raise AssertionError(f"{vm.name}: the host's server said {got!r}")
    print(f"[test] {vm.name} reached the host's loopback through the router")
