"""Instances behind the router: the lease of their index on `vec0`, an IPv6 address each, and reach.

A host forward to an instance answers through the router's egress
(nixidae/nixidae#1609). A terminated instance's index comes back with its
address.
"""

from __future__ import annotations

import anyio
from vivarium_runner import Machine, Machines

SERVE = (
    "py=$(readlink /proc/$(systemctl show -p MainPID --value vivarium-agent)/exe);"
    " mkdir -p /srv && echo hello-$(hostname) > /srv/index.html"
    " && systemd-run --unit=serve -- $py -m http.server 8080 --directory /srv"
)


async def forwarded(vm: Machine) -> str:
    """What the host reads from a forward opened to *vm*'s port 8080 while it runs."""
    await vm.succeed(SERVE)
    await vm.wait_until_succeeds("ss -ltn | grep -q :8080", timeout=30)
    [where] = await vm.open_inbound(8080)
    host, _, port = where.rpartition(":")
    with anyio.fail_after(30):
        async with await anyio.connect_tcp(host, int(port)) as stream:
            await stream.send(b"GET / HTTP/1.0\r\n\r\n")
            got = b""
            async for chunk in stream:
                got += chunk
    return got.decode(errors="replace").splitlines()[-1]


async def addresses(vm: Machine) -> tuple[str, str]:
    """`vec0`'s IPv4 lease and the IPv6 address of the same host part, once
    networkd has both. The kernel adds a temporary IPv6 address besides."""
    await vm.wait_until_succeeds(f"ip -4 -br addr show vec0 | grep -F {vm.uplink_ip}/", timeout=60)
    four = str(vm.uplink_ip)
    six = f"fd00:10:0:2::{four.rsplit('.', 1)[1]}"
    await vm.wait_until_succeeds(f"ip -6 -br addr show vec0 | grep -F {six}/", timeout=60)
    return four, six


async def test(vms: Machines) -> None:
    base = vms.base
    first = await vms.launch("worker", "w1")
    second = await vms.launch("worker", "w2")

    seen = {}
    for vm in (first, second):
        seen[vm.name] = await addresses(vm)
        four, six = seen[vm.name]
        await base.succeed(f"ping -c2 -W5 {four}")
        await base.succeed(f"ping -c2 -W5 {six}")
    if seen["w1"] == seen["w2"]:
        raise RuntimeError(f"w1 and w2 share {seen['w1']}")
    await first.succeed(f"ping -c2 -W5 {seen['w2'][0]}")
    print(f"[test] behind the router: {seen}")

    reply = await forwarded(second)
    if reply != "hello-w2":
        raise RuntimeError(f"the forward to w2 read {reply!r}")
    print("[test] a host forward to an instance answers")

    await vms.terminate("w1")
    third = await vms.launch("worker", "w3")
    if third.spec.index != first.spec.index:
        raise RuntimeError("w3 did not take w1's free index")
    await third.wait_until_succeeds(f"ip -4 -br addr show vec0 | grep -F {seen['w1'][0]}/", timeout=60)
    await base.succeed(f"ping -c2 -W5 {seen['w1'][0]}")
    print("[test] a terminated instance's lease comes back")

    for name in ("w2", "w3"):
        await vms.terminate(name)
