"""Two guests behind the run's router.

Each leases its own address on `vec0` from the router by its MAC, reaches
the host's loopback as 10.0.2.2 through the relay, and reaches the other
guest across the router's bridge.
"""

import asyncio
import socket

from vivarium_runner import Machine, Machines

GUESTS = {"a": "10.0.2.15", "b": "10.0.2.16"}
"""10.0.2.(15 + index); `a` and `b` are indexes 0 and 1."""


async def lease(vm: Machine, address: str) -> None:
    await vm.wait_until_succeeds(f"ip -4 -br addr show vec0 | grep -q ' {address}/24'", timeout=60)
    print(f"[test] {vm.name} leased {address} on vec0")


async def test(vms: Machines) -> None:
    await asyncio.gather(*(lease(vms[name], address) for name, address in GUESTS.items()))

    # A server on the host's loopback, which a guest reaches as the gateway.
    server = socket.create_server(("127.0.0.1", 0))
    port = server.getsockname()[1]
    server.setblocking(False)
    loop = asyncio.get_running_loop()

    async def answer() -> None:
        for _ in GUESTS:
            conn, _ = await loop.sock_accept(server)
            await loop.sock_sendall(conn, b"host\n")
            conn.close()

    answering = asyncio.ensure_future(answer())
    for name in GUESTS:
        got = (await vms[name].succeed(f"timeout 10 bash -c 'exec 3<>/dev/tcp/10.0.2.2/{port}; cat <&3'")).strip()
        if got != "host":
            raise AssertionError(f"{name}: the host's server said {got!r}")
        print(f"[test] {name} reached the host's loopback through the router")
    await asyncio.wait_for(answering, 10)
    server.close()

    await vms.a.succeed(f"ping -c2 -W2 {GUESTS['b']}")
    await vms.b.succeed(f"ping -c2 -W2 {GUESTS['a']}")
    print("[test] a and b reach each other on vec0")
