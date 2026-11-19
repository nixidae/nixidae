"""Two router networks and the uplink, apart.

`d` leases on `lab`, `x` on `other`; each reaches the host through its
own network's relay. A guest bound to its data interface uses that
network's route, whatever the uplink offers. No TCP or UDP flow crosses
the router between `lab`, `other` and the uplink, and a data network
reaches no relay of another network on the router's address.

`m`'s forwards bind the host's 127.0.0.1, as every forward on macOS
does. The uplink's gateway leads there; the egress refuses a flow from
a data network to a forward's port (nixidae/nixidae#1523).
"""

from __future__ import annotations

import socket

import anyio
from vivarium_runner import Machine, Machines

LAB, OTHER = "lab", "other"
LAB_ROUTER = "10.9.0.1"
GATEWAY, GATEWAY6 = "10.0.2.2", "fd00:10:0:2::2"
TCP, UDP = 7777, 7778
UPLINK_RELAY, LAB_RELAY, OTHER_RELAY = 9000, 9002, 9004
RAW = 253
"""An IP protocol for experiments (RFC 3692): no rule on the router names it."""
"""The uplink's relay port, and `other`'s: networks in order of name, 9000 + 2n."""


async def address(vm: Machine, device: str, prefix: str, family: str = "-4") -> str:
    """The address *vm* holds on *device* that starts with *prefix*, once it holds one."""
    await vm.wait_until_succeeds(
        f"ip {family} addr show dev {device} | grep -v tentative | grep -q 'inet6\\? {prefix}'", timeout=60
    )
    out = (await vm.succeed(f"ip {family} -br addr show dev {device}")).stdout.split()
    return next(word.split("/")[0] for word in out[2:] if word.startswith(prefix))


async def serve(vm: Machine) -> None:
    """Answer the guest's name on TCP and UDP, so a probe that reaches it shows."""
    name = "OPEN:/proc/sys/kernel/hostname,rdonly"
    udp_name = "'EXEC:/run/current-system/sw/bin/cat /proc/sys/kernel/hostname'"
    await vm.succeed(f"systemd-run --collect --unit=tcp-{TCP} socat TCP-LISTEN:{TCP},fork,reuseaddr {name}")
    await vm.succeed(f"systemd-run --collect --unit=udp-{UDP} socat UDP4-RECVFROM:{UDP},fork {udp_name}")
    # The control: the server answers on the guest's own addresses.
    await vm.wait_until_succeeds(f"timeout 5 socat -u TCP:127.0.0.1:{TCP} - | grep -qx {vm.name}", timeout=30)


async def probe(vm: Machine, target: str, port: int, device: str | None, udp: bool = False) -> str:
    """What *vm* reads back from *target*:*port*, bound to *device*; empty if nothing answers."""
    bind = f",so-bindtodevice={device}" if device else ""
    if udp:
        cmd = f"echo who | timeout 5 socat -t3 - UDP4:{target}:{port}{bind}"
    else:
        cmd = f"timeout 5 socat -u TCP:{target}:{port}{bind},connect-timeout=3 -"
    return (await vm.execute(cmd)).stdout.strip()


async def refused(vm: Machine, target: str, port: int, device: str | None, why: str, udp: bool = False) -> None:
    got = await probe(vm, target, port, device, udp)
    if got:
        raise AssertionError(f"{vm.name}: {why}: {target}:{port}{'/udp' if udp else ''} answered {got!r}")
    print(f"[test] {vm.name} does not reach {target}:{port}{'/udp' if udp else ''}: {why}")


async def host_through(vm: Machine, device: str, gateway: str, family: socket.AddressFamily) -> None:
    """*vm* bound to *device* reaches a server on the host's loopback as *gateway*."""
    loopback = "::1" if family == socket.AF_INET6 else "127.0.0.1"
    target = f"[{gateway}]" if family == socket.AF_INET6 else gateway
    proto = "TCP6" if family == socket.AF_INET6 else "TCP4"
    with socket.create_server((loopback, 0), family=family) as server:
        server.setblocking(False)
        port = server.getsockname()[1]

        async def answer() -> None:
            await anyio.wait_readable(server)
            conn, _ = server.accept()
            with conn:
                conn.sendall(b"host\n")

        async with anyio.create_task_group() as group:
            group.start_soon(answer)
            got = (
                await vm.succeed(f"timeout 10 socat -u {proto}:{target}:{port},so-bindtodevice={device} -")
            ).stdout.strip()
        if got != "host":
            raise AssertionError(f"{vm.name}: the host's server on {loopback} said {got!r} through {device}")
        print(f"[test] {vm.name} reached the host's {loopback} through {device}")


async def udp_host_through(vm: Machine, device: str) -> None:
    """*vm* bound to *device* gets an echo from a UDP server on the host's loopback."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind(("127.0.0.1", 0))
        udp.setblocking(False)
        port = udp.getsockname()[1]

        async def echo() -> None:
            await anyio.wait_readable(udp)
            data, peer = udp.recvfrom(100)
            udp.sendto(b"host:" + data, peer)

        async with anyio.create_task_group() as group:
            group.start_soon(echo)
            got = (
                await vm.succeed(f"echo hi | timeout 10 socat -t2 - UDP4:{GATEWAY}:{port},so-bindtodevice={device}")
            ).stdout.strip()
            # socat exits 0 with no echo, and `echo` would wait on.
            group.cancel_scope.cancel()
    if got != "host:hi":
        raise AssertionError(f"{vm.name}: the host's UDP echo said {got!r} through {device}")
    print(f"[test] {vm.name} reached the host's 127.0.0.1 over UDP through {device}")


async def raw_arrives(sender: Machine, target: str, device: str | None, receiver: Machine) -> bool:
    """Whether an IP datagram of protocol `RAW` from *sender* to *target* reaches *receiver*."""
    bind = f",so-bindtodevice={device}" if device else ""
    send = f"for i in 1 2 3 4 5 6; do echo {sender.name} | socat -u - IP4-SENDTO:{target}:{RAW}{bind}; sleep 0.5; done"
    got = ""

    async def listen() -> None:
        nonlocal got
        # A raw socket reads the IP header too: keep the printable payload.
        got = (await receiver.execute(f"timeout 4 socat -u IP4-RECV:{RAW} - | tr -cd '[:alnum:]\\n'")).stdout

    async with anyio.create_task_group() as group:
        group.start_soon(listen)
        await sender.execute(send)
    return sender.name in got.split()


async def test(vms: Machines) -> None:
    d, x, m = vms.d, vms.x, vms.m
    d_lab = await address(d, LAB, "10.9.0.")
    x_other = await address(x, OTHER, "10.8.0.")
    d_lab6 = await address(d, LAB, "fd00:9:", "-6")
    m_uplink = await address(m, "vec0", "10.0.2.")
    print(f"[test] d leased {d_lab} and took {d_lab6} on {LAB}; x leased {x_other} on {OTHER}")

    # The data network's default route wins over the uplink's.
    route = (await d.succeed("ip route get 192.0.2.1")).stdout
    if f"via {LAB_ROUTER} dev {LAB}" not in route:
        raise AssertionError(f"d routes off its networks by {route.strip()!r}, not by {LAB_ROUTER} on {LAB}")

    await host_through(d, LAB, GATEWAY, socket.AF_INET)
    await host_through(x, OTHER, GATEWAY, socket.AF_INET)
    await host_through(d, LAB, GATEWAY6, socket.AF_INET6)
    await udp_host_through(d, LAB)
    await udp_host_through(x, OTHER)

    async with anyio.create_task_group() as group:
        for vm in (d, x, m):
            group.start_soon(serve, vm)

    # The control: d on the uplink reaches m's forwards through the gateway.
    for udp in (False, True):
        # The host port of m's forward is the runner's to say: a build binds
        # any free one (nixidae/nixidae#1546).
        [where] = m.reachable(UDP if udp else TCP)
        port = int(where.rpartition(":")[2])
        got = await probe(d, GATEWAY, port, "vec0", udp)
        if got != m.name:
            raise AssertionError(f"d: m's forward {GATEWAY}:{port}{'/udp' if udp else ''} on the uplink said {got!r}")
        print(f"[test] d reaches m's forward {GATEWAY}:{port}{'/udp' if udp else ''} on the uplink")
        await refused(d, GATEWAY, port, LAB, "lab into m's forward", udp)
        await refused(x, GATEWAY, port, OTHER, "other into m's forward", udp)

    for udp in (False, True):
        port = UDP if udp else TCP
        await refused(d, x_other, port, LAB, "lab to other", udp)
        await refused(x, d_lab, port, OTHER, "other to lab", udp)
        await refused(d, m_uplink, port, LAB, "lab to the uplink", udp)
        await refused(m, d_lab, port, None, "the uplink to lab", udp)

    # A protocol no rule on the router names reaches no relay: only the
    # router's routing could carry it. The router forwards nothing, and
    # each VRF has no route to another network. The uplink, which d and m
    # share, is the control.
    if not await raw_arrives(d, m_uplink, None, m):
        raise AssertionError(f"d: protocol {RAW} to m on the shared uplink did not arrive")
    print(f"[test] d reaches m with protocol {RAW} on the uplink they share")
    for sender, target, device, receiver, why in (
        (d, x_other, LAB, x, "lab to other"),
        (x, d_lab, OTHER, d, "other to lab"),
        (d, m_uplink, LAB, m, "lab to the uplink"),
        (m, d_lab, None, d, "the uplink to lab"),
    ):
        if await raw_arrives(sender, target, device, receiver):
            raise AssertionError(f"{sender.name}: protocol {RAW} to {target} crossed the router: {why}")
        print(f"[test] {sender.name} does not reach {target} with protocol {RAW}: {why}")

    # The router's sockets in other VRFs, on lab's router address: each
    # relay binds to its own. Lab's relay is the control: it accepts, and
    # resets a connection that has no original destination.
    for port, opens in ((LAB_RELAY, True), (UPLINK_RELAY, False), (OTHER_RELAY, False)):
        out = await connect(d, LAB_ROUTER, port)
        if opens == ("Connection refused" in out):
            raise AssertionError(f"d: {LAB_ROUTER}:{port} on the router: {out!r}")
        print(f"[test] d {'opens' if opens else 'is refused'} {LAB_ROUTER}:{port} on the router")


async def connect(vm: Machine, target: str, port: int) -> str:
    """socat's messages for a TCP connection to *target*:*port*."""
    return (await vm.execute(f"timeout 5 socat -u TCP:{target}:{port},connect-timeout=3 /dev/null 2>&1")).stdout.strip()
