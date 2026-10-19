"""Rekey under load, and lose nothing.

IKEv2 rekeys make-before-break: the new CHILD_SA is installed before the
old one is deleted. So a ping every 100 ms across a CHILD_SA rekey and
an IKE_SA rekey loses no packet, the SPIs all change, and the new
CHILD_SA shows the ECP_384 group, because a rekey does a fresh DH (PFS)
where the first CHILD_SA rode on IKE_AUTH's keys. On the routed tunnel
the BGP session over it never drops.
"""

import anyio

from lab import child, established, lab, loss, ping, vtysh
from vivarium_runner import Machine, Machines


async def rekey_under_load(
    gateway: Machine, sender: Machine, target: str, connection: str
) -> tuple[dict, dict, dict, dict]:
    before = await established(gateway, connection)
    old = child(before, connection)
    assert old is not None
    outcome: dict[str, str] = {}

    async def load() -> None:
        outcome["ping"] = await sender.succeed(ping(target, count=60, extra="-i 0.1"), timeout=30)

    async with anyio.create_task_group() as tg:
        tg.start_soon(load)
        await anyio.sleep(1.5)
        await gateway.succeed(f"swanctl --rekey --child {connection}")
        await anyio.sleep(1.5)
        await gateway.succeed(f"swanctl --rekey --ike {connection}")

    sent, received = loss(outcome["ping"])
    if sent != received:
        raise AssertionError(f"{connection}: {sent - received} of {sent} pings lost across the rekeys")
    after = await established(gateway, connection)
    new = child(after, connection)
    assert new is not None
    return before, old, after, new


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    hosts = cfg["hosts"]

    peer = cfg["tunnels"]["1"]["east"]
    flaps = (await vtysh(vms.west, f"show bgp neighbors {peer}"))[peer]["connectionsEstablished"]

    for connection, sender, target in (
        ("policy", vms.alice, hosts["bob"]["address"]),
        ("tunnel1", vms.carol, hosts["dave"]["address"]),
    ):
        before, old, after, new = await rekey_under_load(vms.west, sender, target, connection)
        if (new["spi-in"], new["spi-out"]) == (old["spi-in"], old["spi-out"]):
            raise AssertionError(f"{connection}: CHILD_SA SPIs unchanged after rekey: {new}")
        if (after["initiator-spi"], after["responder-spi"]) == (before["initiator-spi"], before["responder-spi"]):
            raise AssertionError(f"{connection}: IKE_SA SPIs unchanged after rekey")
        if new.get("dh-group") != "ECP_384":
            raise AssertionError(f"{connection}: rekeyed CHILD_SA has no PFS group: {new}")
        print(
            f"[test] {connection}: CHILD_SA {old['spi-out']} -> {new['spi-out']} with PFS ECP_384, "
            f"IKE_SA {before['initiator-spi']} -> {after['initiator-spi']}, 60/60 pings"
        )

    now = (await vtysh(vms.west, f"show bgp neighbors {peer}"))[peer]
    if now["bgpState"] != "Established" or now["connectionsEstablished"] != flaps:
        raise AssertionError(f"BGP over tunnel1 flapped during its rekey: {now}")
    print("[test] BGP over tunnel1 stayed up through both rekeys")
