"""Instances behind the router: the lease of their index on `vec0`, an IPv6 address each, and reach.

A terminated instance's index comes back with its address.
"""

from __future__ import annotations

from vivarium_runner import Machine, Machines


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

    await vms.terminate("w1")
    third = await vms.launch("worker", "w3")
    if third.spec.index != first.spec.index:
        raise RuntimeError("w3 did not take w1's free index")
    await third.wait_until_succeeds(f"ip -4 -br addr show vec0 | grep -F {seen['w1'][0]}/", timeout=60)
    await base.succeed(f"ping -c2 -W5 {seen['w1'][0]}")
    print("[test] a terminated instance's lease comes back")

    for name in ("w2", "w3"):
        await vms.terminate(name)
