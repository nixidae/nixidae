"""Instances of one template: each its own name, address and user data; a terminated one's slot comes back.

nixidae/nixidae#1408.
"""

from __future__ import annotations

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    base = vms.base
    first = await vms.launch("worker", "w1", user_data="token-1")
    second = await vms.launch("worker", "w2", user_data="token-2")

    for vm, address, token in ((first, "10.9.0.100", "token-1"), (second, "10.9.0.101", "token-2")):
        await vm.succeed(f"test $(hostname) = {vm.name}")
        # networkd applies the instance's address on its own time, after boot.
        await vm.wait_until_succeeds(f"ip -4 -br addr show eth1 | grep -F {address}", timeout=30)
        assert (await vm.succeed("cat /run/vivarium/user-data")).stdout == token
        await base.succeed(f"ping -c2 -W5 {address}")
    await first.succeed("ping -c2 -W5 10.9.0.101")
    print("[test] two instances, each with its own name, address and user data, reach each other")

    await vms.terminate("w1")
    assert "w1" not in vms
    assert not first.alive(), "w1 still runs after terminate"
    await base.fail("ping -c1 -W2 10.9.0.100")

    third = await vms.launch("worker", "w3")
    assert third.spec.index == first.spec.index, "w3 did not take w1's free index"
    await third.wait_until_succeeds("ip -4 -br addr show eth1 | grep -F 10.9.0.100", timeout=30)
    await third.fail("test -e /run/vivarium/user-data")
    await base.succeed("ping -c2 -W5 10.9.0.100")
    print("[test] a terminated instance's index and address come back")

    for name in ("w2", "w3"):
        await vms.terminate(name)
    assert sorted(vms) == ["base"]
