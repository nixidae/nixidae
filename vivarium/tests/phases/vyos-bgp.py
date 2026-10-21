"""eBGP between VyOS and FRR, and a path between two LANs that only BGP
built. VyOS runs the vivarium agent, so its FRR answers vtysh as FRR's
does."""

import json

import anyio

from vivarium_runner import Machine, Machines

CONVERGE = 120


async def established(vm: Machine, peer: str) -> bool:
    peers = json.loads(await vm.succeed("vtysh -c 'show bgp ipv4 unicast summary json'")).get("peers", {})
    return peers.get(peer, {}).get("state") == "Established"


async def test(vms: Machines) -> None:
    r1, f1, a, b = vms.r1, vms.f1, vms.a, vms.b

    links = await r1.succeed("ip -br link show eth1; ip -br link show eth2")
    for name, mac in (("eth1", r1.spec.interfaces[0].mac), ("eth2", r1.spec.interfaces[1].mac)):
        if name not in links or mac.lower() not in links.lower():
            raise AssertionError(f"VyOS has no {name} with MAC {mac}:\n{links}")
    print("[test] VyOS's interfaces carry their vivarium names")

    with anyio.fail_after(CONVERGE):
        while not (await established(f1, "10.0.0.1") and await established(r1, "10.0.0.2")):
            await anyio.sleep(1)
    print("[test] the session is up on both ends")

    for vm, prefix in ((r1, "10.2.0.0/24"), (f1, "10.1.0.0/24")):
        with anyio.fail_after(CONVERGE):
            while True:
                routes = json.loads(await vm.succeed(f"vtysh -c 'show ip route {prefix} json'"))
                if routes.get(prefix, [{}])[0].get("protocol") == "bgp":
                    break
                await anyio.sleep(1)
    print("[test] each router learned the other's LAN over BGP")

    for src, dst in ((a, "10.2.0.2"), (b, "10.1.0.2")):
        out = await src.succeed(f"ping -c 3 -W 2 {dst}")
        if "ttl=62" not in out:
            raise AssertionError(f"{src.name} reached {dst} other than through both routers:\n{out}")
    print("[test] a and b reach each other through both routers")
