"""VyOS behind the router: eth0 leases the router's address, and VyOS reaches the host through the relay."""

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


async def lease(vyos: Machine) -> ipaddress.IPv4Address:
    """eth0's address from the router. vyos-router starts the DHCP client
    well after the agent is up, so name the stage on a timeout."""
    try:
        text = (await vyos.wait_until_succeeds(LEASE, timeout=300)).stdout.strip()
    except MachineError as error:
        status = (await vyos.execute("systemctl --no-pager status vyos-router 2>&1")).stdout
        raise AssertionError(f"{error}\nvyos-router:\n{status}") from None
    return ipaddress.IPv4Address(text)


async def test(vms: Machines) -> None:
    vyos = vms.vyos
    address = await lease(vyos)
    if str(address) != vyos.uplink_ip:
        raise AssertionError(f"vyos leased {address}, and the router gives it {vyos.uplink_ip}")
    print(f"[test] vyos leased {address} on eth0 from the router")

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    port = listener.extra(SocketAttribute.local_port)

    async def accept() -> None:
        await listener.serve(serve)

    async with listener, anyio.create_task_group() as group:
        group.start_soon(accept)
        got = (await vyos.succeed(f"curl -sS --max-time 10 http://{GATEWAY}:{port}/")).stdout.strip()
        group.cancel_scope.cancel()
    if got != BODY.decode():
        raise AssertionError(f"vyos: the host's server said {got!r}")
    print("[test] vyos reached the host's loopback through the router")
