"""RouterOS behind the router: its DHCP client on ether1 leases the router's address, and it reaches the host through the relay."""

from __future__ import annotations

import ipaddress

import anyio
from anyio.abc import SocketAttribute, SocketStream
from vivarium_runner import Machine, Machines

GATEWAY = "10.0.2.2"
"""The uplink's gateway: the egress maps it to the host's loopback (nix/uplink.nix)."""

UPLINK = ipaddress.ip_network("10.0.2.0/24")

BODY = b"host"


async def serve(stream: SocketStream) -> None:
    async with stream:
        request = b""
        while b"\r\n\r\n" not in request:
            request += await stream.receive()
        await stream.send(b"HTTP/1.0 200 OK\r\nContent-Length: %d\r\n\r\n%s" % (len(BODY), BODY))


async def leased(vm: Machine) -> ipaddress.IPv4Interface | None:
    done = await vm.execute(":put [/ip dhcp-client get [find interface=ether1] address]")
    text = done.stdout.strip()
    if done.returncode != 0 or not text:
        return None
    address = ipaddress.IPv4Interface(text)
    return address if address.ip in UPLINK else None


async def test(vms: Machines) -> None:
    r1 = vms.r1
    address = await r1.until("a lease on ether1", lambda: leased(r1), timeout=60)
    print(f"[test] r1 leased {address} from the router")

    listener = await anyio.create_tcp_listener(local_host="127.0.0.1")
    port = listener.extra(SocketAttribute.local_port)

    async def accept() -> None:
        await listener.serve(serve)

    async with listener, anyio.create_task_group() as group:
        group.start_soon(accept)
        url = f"http://{GATEWAY}:{port}/"
        got = (await r1.succeed(f':put ([/tool fetch url="{url}" output=user as-value]->"data")')).stdout.strip()
        group.cancel_scope.cancel()
    if got != BODY.decode():
        raise AssertionError(f"r1: the host's server said {got!r}")
    print("[test] r1 reached the host's loopback through the router")
