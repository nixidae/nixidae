"""Inbound forwards added and removed while the guest runs (nixidae/nixidae#1546).

The egress takes a socket the runner binds at any time and gives one
back on `remove`. What is worth guarding:

  * an added port answers, TCP and UDP;
  * a removed port refuses new connections, and an open flow on it
    ends rather than lingering;
  * the guest port can be forwarded again after its removal.
"""

from __future__ import annotations

import socket
from collections.abc import Callable

import anyio
from vivarium_runner import Machine, Machines

TCP = 9190
UDP = 9191
WITHIN = 30.0


def _hello(where: str) -> bytes:
    host, _, port = where.rpartition(":")
    with socket.create_connection((host, int(port)), timeout=5) as conn:
        conn.sendall(b"hello\n")
        return conn.recv(64)


def _echo(where: str) -> bytes:
    host, _, port = where.rpartition(":")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.settimeout(2)
        sock.sendto(b"ping", (host, int(port)))
        return sock.recv(64)


async def answers(call: Callable[[str], bytes], where: str) -> bytes:
    """*call* on *where* until it answers, giving the guest time to bind:
    the egress accepts and then closes while nothing listens in the guest."""
    with anyio.fail_after(WITHIN):
        while True:
            try:
                if got := await anyio.to_thread.run_sync(call, where):
                    return got
            except OSError:
                pass
            await anyio.sleep(0.5)


async def serve(vm: Machine) -> None:
    await vm.succeed(f"systemd-run --unit=echo-tcp --collect socat TCP-LISTEN:{TCP},fork,reuseaddr PIPE")
    await vm.succeed(f"systemd-run --unit=echo-udp --collect socat UDP4-RECVFROM:{UDP},fork PIPE")


async def test(vms: Machines) -> None:
    vm = vms.node
    await serve(vm)
    if vm.reachable(TCP):
        raise AssertionError(f"{TCP} is forwarded before anyone asked")

    [where] = await vm.open_inbound(TCP)
    if await answers(_hello, where) != b"hello\n":
        raise AssertionError(f"{where} did not echo")
    print(f"[test] added {TCP}: answers on {where}")

    # A flow open across the removal: it must end, not hang.
    host, _, port = where.rpartition(":")
    held = socket.create_connection((host, int(port)), timeout=5)
    held.sendall(b"one\n")
    if held.recv(64) != b"one\n":
        raise AssertionError("the held flow did not echo")
    await vm.close_inbound(TCP)
    if vm.reachable(TCP):
        raise AssertionError(f"{TCP} is still forwarded after its removal")
    held.settimeout(10)
    try:
        ended = held.recv(64) == b""
    except (ConnectionResetError, TimeoutError):
        ended = True
    finally:
        held.close()
    if not ended:
        raise AssertionError("the open flow outlived its forward")
    try:
        await anyio.to_thread.run_sync(_hello, where)
    except OSError:
        pass
    else:
        raise AssertionError(f"{where} still answers after its removal")
    print(f"[test] removed {TCP}: the open flow ended and {where} refuses")

    [where] = await vm.open_inbound(UDP, protocols=("udp",))
    if await answers(_echo, where) != b"ping":
        raise AssertionError(f"{where}/udp did not echo")
    await vm.close_inbound(UDP)
    try:
        await anyio.to_thread.run_sync(_echo, where)
    except OSError:
        pass
    else:
        raise AssertionError(f"{where}/udp still answers after its removal")
    print(f"[test] added and removed {UDP}/udp on {where}")

    [again] = await vm.open_inbound(TCP)
    if await answers(_hello, again) != b"hello\n":
        raise AssertionError(f"{again} did not echo after a second add")
    print(f"[test] {TCP} forwarded again on {again}")
