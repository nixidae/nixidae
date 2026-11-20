"""Two guests behind the run's router.

Each leases its own address on `vec0` from the router by its MAC, and
takes its IPv6 address from the router's advertisement. It reaches the
host's loopback as 10.0.2.2 and fd00:10:0:2::2 through the relay, and the
other guest across the router's bridge. The host reaches each guest on the
addresses `reachable` names, through the router's egress.
"""

from __future__ import annotations

import socket

import anyio
from vivarium_runner import Machine, Machines

GUESTS = {"a": "10.0.2.15", "b": "10.0.2.16"}
"""10.0.2.(15 + index); `a` and `b` are indexes 0 and 1."""
GUESTS6 = {"a": "fd00:10:0:2::15", "b": "fd00:10:0:2::16"}
GATEWAY6 = "fd00:10:0:2::2"


def ping_sockets(family: socket.AddressFamily, proto: int) -> bool:
    try:
        socket.socket(family, socket.SOCK_DGRAM, proto).close()
    except PermissionError:
        return False
    return True


async def answer(server: socket.socket, reply: bool) -> None:
    """Accept one connection per guest. Send `host`, or with *reply* the peer's address."""
    for _ in GUESTS:
        await anyio.wait_readable(server)
        conn, peer = server.accept()
        with conn:
            conn.sendall(f"{peer[0]}\n".encode() if reply else b"host\n")


async def lease(vm: Machine, address: str, address6: str) -> None:
    await vm.wait_until_succeeds(f"ip -4 -br addr show vec0 | grep -q ' {address}/24'", timeout=60)
    # Not while tentative: duplicate detection holds a new address back.
    await vm.wait_until_succeeds(
        f"ip -6 addr show dev vec0 | grep -v tentative | grep -q 'inet6 {address6}/64'", timeout=60
    )
    print(f"[test] {vm.name} leased {address} and took {address6} on vec0")


async def test(vms: Machines) -> None:
    async with anyio.create_task_group() as group:
        for name in GUESTS:
            group.start_soon(lease, vms[name], GUESTS[name], GUESTS6[name])

    # A server on the host's loopback, which a guest reaches as the gateway.
    # A guest's reply comes after the server sent it, so the group ends once
    # the guests have their answers.
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.setblocking(False)
        port = server.getsockname()[1]
        async with anyio.create_task_group() as group:
            group.start_soon(answer, server, False)
            for name in GUESTS:
                got = (
                    await vms[name].succeed(f"timeout 10 bash -c 'exec 3<>/dev/tcp/10.0.2.2/{port}; cat <&3'")
                ).stdout.strip()
                if got != "host":
                    raise AssertionError(f"{name}: the host's server said {got!r}")
                print(f"[test] {name} reached the host's loopback through the router")

    await ipv6(vms)

    # UDP and ICMP off the bridge reach the relay only by this rule; the
    # sandbox has no outside to show its loss.
    await vms.router.succeed("ip rule | grep -q 'fwmark 0x1 lookup 100'")
    await vms.router.succeed("ip -6 rule | grep -q 'fwmark 0x1 lookup 100'")

    # Echo through the relay and the egress's ping socket, to the host. The
    # egress shares this process's network namespace; the build sandbox's
    # refuses ping sockets.
    if ping_sockets(socket.AF_INET, socket.IPPROTO_ICMP):
        await vms.a.succeed("ping -c2 -W2 10.0.2.2")
        print("[test] a pinged the gateway through the router")
    else:
        print("[test] no ping sockets on this host side; echo not checked")
    if ping_sockets(socket.AF_INET6, socket.IPPROTO_ICMPV6):
        await vms.a.succeed(f"ping -6 -c2 -W2 {GATEWAY6}")
        print("[test] a pinged the IPv6 gateway through the router")
    else:
        print("[test] no IPv6 ping sockets on this host side; echo not checked")

    await vms.a.succeed(f"ping -c2 -W2 {GUESTS['b']}")
    await vms.b.succeed(f"ping -c2 -W2 {GUESTS['a']}")
    await vms.a.succeed(f"ping -6 -c2 -W2 {GUESTS6['b']}")
    await vms.b.succeed(f"ping -6 -c2 -W2 {GUESTS6['a']}")
    print("[test] a and b reach each other on vec0, IPv4 and IPv6")

    await forwards(vms)


async def ipv6(vms: Machines) -> None:
    """The host's ::1 as the IPv6 gateway, TCP and UDP, through the relay."""
    with socket.create_server(("::1", 0), family=socket.AF_INET6) as server:
        server.setblocking(False)
        port = server.getsockname()[1]
        async with anyio.create_task_group() as group:
            group.start_soon(answer, server, True)
            for name in GUESTS:
                got = (await vms[name].succeed(f"timeout 10 socat -u TCP6:[{GATEWAY6}]:{port} -")).stdout.strip()
                # The egress connects from the host's own ::1.
                if got != "::1":
                    raise AssertionError(f"{name}: the host's IPv6 server saw {got!r}")
                print(f"[test] {name} reached the host's ::1 through the router")

    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as udp:
        udp.bind(("::1", 0))
        udp.setblocking(False)
        port = udp.getsockname()[1]

        async def echo() -> None:
            await anyio.wait_readable(udp)
            data, peer = udp.recvfrom(100)
            udp.sendto(b"host:" + data, peer)

        async with anyio.create_task_group() as group:
            group.start_soon(echo)
            got = (await vms.a.succeed(f"echo hi | timeout 10 socat -t1 - UDP6:[{GATEWAY6}]:{port}")).stdout.strip()
            # socat exits 0 with no echo, and `echo` would wait on.
            group.cancel_scope.cancel()
        if got != "host:hi":
            raise AssertionError(f"a: the host's IPv6 UDP echo said {got!r}")
        print("[test] a reached the host's ::1 over UDP through the router")

    # Nothing answers an IPv6 connection the egress cannot make.
    rc = (await vms.a.execute("timeout 10 socat -u TCP6:[2001:db8::1]:80 /dev/null")).returncode
    if rc == 0:
        raise AssertionError("a connected to 2001:db8::1, which no host has")


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
    deadline = anyio.current_time() + timeout
    while True:
        try:
            got = await anyio.to_thread.run_sync(_ask_udp if udp else _ask_tcp, where)
        except OSError as error:
            got, why = "", str(error)
        else:
            why = "an empty answer"
        if got:
            return got
        if anyio.current_time() > deadline:
            raise AssertionError(f"nothing answered on {where}: {why}")
        await anyio.sleep(0.5)


async def forwards(vms: Machines) -> None:
    """The host reaches each guest on its own forwards, through the egress."""
    for name in GUESTS:
        await vms[name].succeed(f"systemd-run --collect --unit=tcp-{TCP} socat TCP-LISTEN:{TCP},fork,reuseaddr {NAME}")
    await vms.a.succeed(
        f"systemd-run --collect --unit=tcp-{PRIVILEGED} socat TCP-LISTEN:{PRIVILEGED},fork,reuseaddr {NAME}"
    )
    await vms.a.succeed(f"systemd-run --collect --unit=udp-{UDP} socat UDP4-RECVFROM:{UDP},fork {UDP_NAME}")

    checks = [(name, TCP, False) for name in GUESTS] + [("a", PRIVILEGED, False), ("a", UDP, True)]
    for name, port, udp in checks:
        where = vms[name].reachable(port, "udp" if udp else "tcp")
        if not where:
            raise AssertionError(f"{name}: guest port {port} is not forwarded")
        got = await ask(where[0], udp=udp)
        # A wrong address or offset answers with the other guest's name.
        if got != name:
            raise AssertionError(f"{name}: {where[0]} answered {got!r}, not {name!r}")
        print(f"[test] the host reached {name}:{port}{'/udp' if udp else ''} on {where[0]}")
    if vms.b.reachable(PRIVILEGED):
        raise AssertionError("b forwards only its one port, and port 80 is not it")
