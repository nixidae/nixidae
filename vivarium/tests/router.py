"""Two guests behind the run's router.

Each leases its own address on `vec0` from the router by its MAC, reaches
the host's loopback as 10.0.2.2 through the relay, and reaches the other
guest across the router's bridge. The host reaches each guest on the
addresses `reachable` names, through the router's egress.
"""

import asyncio
import socket

from vivarium_runner import Machine, Machines

GUESTS = {"a": "10.0.2.15", "b": "10.0.2.16"}
"""10.0.2.(15 + index); `a` and `b` are indexes 0 and 1."""


def ping_sockets() -> bool:
    try:
        socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP).close()
    except PermissionError:
        return False
    return True


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

    # UDP and ICMP off the bridge reach the relay only by this rule; the
    # sandbox has no outside to show its loss.
    await vms.router.succeed("ip rule | grep -q 'fwmark 0x1 lookup 100'")

    # Echo through the relay and the egress's ping socket, to the host. The
    # egress shares this process's network namespace; the build sandbox's
    # refuses ping sockets, as it does passt's.
    if ping_sockets():
        await vms.a.succeed("ping -c2 -W2 10.0.2.2")
        print("[test] a pinged the gateway through the router")
    else:
        print("[test] no ping sockets on this host side; echo not checked")

    await vms.a.succeed(f"ping -c2 -W2 {GUESTS['b']}")
    await vms.b.succeed(f"ping -c2 -W2 {GUESTS['a']}")
    print("[test] a and b reach each other on vec0")

    await forwards(vms)


TCP = 7777
UDP = 7778
PRIVILEGED = 80
NAME = "OPEN:/proc/sys/kernel/hostname,rdonly"
"""The guest answers its name. A transient unit's PATH has no `hostname` or `cat`."""
UDP_NAME = "'EXEC:/run/current-system/sw/bin/cat /proc/sys/kernel/hostname'"
"""For UDP the child must read the datagram, or the forking parent finds it
again and forks without end: `-U` with `NAME` sent 300 replies a second."""


def _ask_tcp(where: str) -> str:
    host, port = where.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=5) as conn:
        return conn.makefile().readline().strip()


def _ask_udp(where: str) -> str:
    host, port = where.rsplit(":", 1)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.sendto(b"who\n", (host, int(port)))
        return sock.recv(100).decode().strip()


async def ask(where: str, udp: bool = False, timeout: float = 30) -> str:
    """The guest's answer on *where*, giving its server time to bind."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        try:
            got = await asyncio.to_thread(_ask_udp if udp else _ask_tcp, where)
        except OSError as error:
            got, why = "", str(error)
        else:
            why = "an empty answer"
        if got:
            return got
        if loop.time() > deadline:
            raise AssertionError(f"nothing answered on {where}: {why}")
        await asyncio.sleep(0.5)


async def forwards(vms: Machines) -> None:
    """The host reaches each guest on its own forwards, through the egress."""
    for name in GUESTS:
        await vms[name].succeed(f"systemd-run --collect --unit=tcp-{TCP} socat TCP-LISTEN:{TCP},fork,reuseaddr {NAME}")
    await vms.a.succeed(f"systemd-run --collect --unit=tcp-{PRIVILEGED} socat TCP-LISTEN:{PRIVILEGED},fork,reuseaddr {NAME}")
    await vms.a.succeed(f"systemd-run --collect --unit=udp-{UDP} socat UDP4-RECVFROM:{UDP},fork {UDP_NAME}")

    checks = [(name, TCP, False) for name in GUESTS] + [("a", PRIVILEGED, False), ("a", UDP, True)]
    for name, port, udp in checks:
        where = vms[name].reachable(port)
        if not where:
            raise AssertionError(f"{name}: guest port {port} is not forwarded")
        got = await ask(where[0], udp=udp)
        # A wrong address or offset answers with the other guest's name.
        if got != name:
            raise AssertionError(f"{name}: {where[0]} answered {got!r}, not {name!r}")
        print(f"[test] the host reached {name}:{port}{'/udp' if udp else ''} on {where[0]}")
    if vms.b.reachable(PRIVILEGED):
        raise AssertionError("b forwards only its one port, and port 80 is not it")
