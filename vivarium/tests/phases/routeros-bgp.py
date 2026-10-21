"""eBGP between RouterOS and FRR, and a path between two LANs that only
BGP built."""

import json

import anyio

from vivarium_runner import Machines

CONVERGE = 60


async def test(vms: Machines) -> None:
    r1, f1, a, b = vms.r1, vms.f1, vms.a, vms.b

    with anyio.fail_after(CONVERGE):
        while True:
            peers = json.loads(await f1.succeed("vtysh -c 'show bgp ipv4 unicast summary json'"))["peers"]
            routeros = await r1.succeed("/routing bgp session print terse where remote.address=10.0.0.2")
            if peers.get("10.0.0.1", {}).get("state") == "Established" and " E " in f" {routeros} ":
                break
            await anyio.sleep(1)
    print("[test] the session is up on both ends")

    with anyio.fail_after(CONVERGE):
        while "10.2.0.0/24" not in (learned := await r1.succeed("/ip route print terse where bgp")):
            await anyio.sleep(1)
    print(f"[test] RouterOS learned FRR's LAN: {learned.strip()}")

    routes = json.loads(await f1.succeed("vtysh -c 'show ip route 10.1.0.0/24 json'"))
    if routes.get("10.1.0.0/24", [{}])[0].get("protocol") != "bgp":
        raise AssertionError(f"FRR has no BGP route to RouterOS's LAN: {routes}")
    print("[test] FRR learned RouterOS's LAN")

    for src, dst in ((a, "10.2.0.2"), (b, "10.1.0.2")):
        out = await src.succeed(f"ping -c 3 -W 2 {dst}")
        # TTL 64 from a Linux guest, less one per router.
        if "ttl=62" not in out:
            raise AssertionError(f"{src.name} reached {dst} other than through both routers:\n{out}")
    print("[test] a and b reach each other through both routers")
