"""A dinix guest: dinit is PID 1, the store is mounted from the host, and
the agent answers for dinit. Its uplink leases an address from the
router, and the host reaches a server through it."""

from __future__ import annotations

import socket

import anyio
from vivarium_runner import Machines

WEB = 8080
"""`web` in tests/dinix: busybox httpd, serving "dinix"."""

GATEWAY = "10.0.2.2"
"""The uplink's gateway: the egress maps it to the host's loopback (nix/uplink.nix)."""


async def answer(server: socket.socket) -> None:
    await anyio.wait_readable(server)
    conn, _ = server.accept()
    with conn:
        conn.sendall(b"host\n")


async def fetch(address: str) -> bytes:
    host, port = address.rsplit(":", 1)
    async with await anyio.connect_tcp(host, int(port)) as stream:
        await stream.send(b"GET / HTTP/1.0\r\n\r\n")
        body = b""
        async for chunk in stream:
            body += chunk
    return body


async def test(vms: Machines) -> None:
    box = vms.box
    init = (await box.succeed("cat /proc/1/comm")).stdout.strip()
    if init != "dinit":
        raise AssertionError(f"PID 1 is {init!r}, not dinit")
    mounts = (await box.succeed("cat /proc/mounts")).stdout
    # A VM mounts /nix, a container /nix/store.
    if not any(line.split()[1] in ("/nix", "/nix/store") for line in mounts.splitlines()):
        raise AssertionError(f"no store mount in /proc/mounts:\n{mounts}")
    print("[test] dinit is PID 1, with the host's store")

    # Behind the router the guest leases its address; on its own relay it
    # has the uplink's fixed one.
    want = box.spec.uplink_address or box.spec.uplink_network.address

    async def leased() -> str | None:
        found = (await box.succeed("ip -4 -o addr show dev vec0")).stdout
        return found if f" {want}/" in found else None

    print(f"[test] vec0 leased {want}: {(await box.until('vec0 to lease', leased, timeout=30, poll=0.2)).strip()}")
    # The uplink found its NIC on the first start: the backend made vec0
    # before the init ran. A retry costs dinit's 5 s restart delay.
    missed = [line for line in box.console_tail(2000) if "no NIC has the uplink's MAC" in line]
    if missed:
        raise AssertionError(f"the uplink started before vec0 existed: {missed}")
    print("[test] vec0 was there when the uplink started")

    async def served() -> bytes | None:
        try:
            body = await fetch(box.reachable(WEB)[0])
        except OSError:
            return None
        return body if body.endswith(b"dinix\n") else None

    await box.until("the host to reach web", served, timeout=30, poll=0.5)
    print(f"[test] the host reached web through {box.reachable(WEB)[0]}")

    # Outbound: a server on the host's loopback, which the guest reaches as
    # the gateway.
    with socket.create_server(("127.0.0.1", 0)) as server:
        server.setblocking(False)
        port = server.getsockname()[1]
        async with anyio.create_task_group() as group:
            group.start_soon(answer, server)
            # busybox nc waits on an open stdin after the host closes.
            got = (await box.succeed(f"timeout 10 nc {GATEWAY} {port} </dev/null")).stdout.strip()
            # Nothing may have connected: the answer then waits forever.
            group.cancel_scope.cancel()
    if got != "host":
        raise AssertionError(f"the host's server said {got!r}")
    print("[test] the guest reached the host's loopback through its uplink")

    # The boot phase returned, so `slow` had started: boot waits for it.
    slow = (await box.succeed("dinitctl is-started slow")).stdout
    if slow.strip() != "STARTED":
        raise AssertionError(f"the boot phase ended with slow {slow.strip()}")
    print("[test] the boot phase waited for a service that takes a second")

    done = await box.execute("dinitctl start broken")
    if done.returncode == 0:
        raise AssertionError(f"broken started: {done.stdout}")
    report = await box.system_state(timeout=10)
    if report != {"state": "degraded", "failed": ("broken",)}:
        raise AssertionError(f"with broken failed, the guest reports {report}")
    print(f"[test] a failed service shows: {report}")

    # A daemon that exits 1 after it started: dinit calls it terminated.
    await box.succeed("dinitctl start dies")

    async def stopped() -> str | None:
        status = (await box.succeed("dinitctl status dies")).stdout
        return status if "STOPPED" in status else None

    await box.until("dies to exit", stopped, timeout=10, poll=0.1)
    report = await box.system_state(timeout=10)
    if report != {"state": "degraded", "failed": ("broken", "dies")}:
        raise AssertionError(f"with dies exited 1, the guest reports {report}")
    print(f"[test] a daemon that exits 1 shows: {report}")

    try:
        await box.unit_state("vivarium-agent")
    except RuntimeError as error:
        if "asks systemd" not in str(error):
            raise
        print(f"[test] a systemd call refuses: {error}")
    else:
        raise AssertionError("unit_state answered on a dinit guest")
