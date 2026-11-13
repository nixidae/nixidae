"""eBGP between VyOS and FRR, and a path between two LANs that only BGP
built. VyOS runs the vivarium agent, so its FRR answers vtysh as FRR's
does."""

import json
from typing import TypedDict

from vivarium_runner import Machine, Machines

CONVERGE = 120


class BgpPeer(TypedDict, total=False):
    state: str


class BgpSummary(TypedDict, total=False):
    """`show bgp ipv4 unicast summary json`. FRR omits `peers` with no neighbours."""

    peers: dict[str, BgpPeer]


class RouteEntry(TypedDict, total=False):
    protocol: str


PEERS = {"f1": "10.0.0.1", "r1": "10.0.0.2"}
"""Each router's BGP neighbour."""

LANS = {"r1": "10.2.0.0/24", "f1": "10.1.0.0/24"}
"""The LAN each router learns only over BGP."""


async def established(vm: Machine) -> bool | None:
    # vtysh exits 1 until VyOS has started FRR.
    rc, out = await vm.execute("vtysh -c 'show bgp ipv4 unicast summary json'")
    if rc != 0:
        return None
    summary: BgpSummary = json.loads(out)
    neighbour = summary.get("peers", {}).get(PEERS[vm.name], {})
    return True if neighbour.get("state") == "Established" else None


async def learned(vm: Machine) -> bool | None:
    prefix = LANS[vm.name]
    routes: dict[str, list[RouteEntry]] = json.loads(await vm.succeed(f"vtysh -c 'show ip route {prefix} json'"))
    return True if routes.get(prefix, [{}])[0].get("protocol") == "bgp" else None


async def test(vms: Machines) -> None:
    r1, a, b = vms.r1, vms.a, vms.b

    links = await r1.succeed("ip -br link show eth1; ip -br link show eth2")
    for name, mac in (("eth1", r1.spec.interfaces[0].mac), ("eth2", r1.spec.interfaces[1].mac)):
        if name not in links or mac.lower() not in links.lower():
            raise AssertionError(f"VyOS has no {name} with MAC {mac}:\n{links}")
    print("[test] VyOS's interfaces carry their vivarium names")

    await vms.until("the BGP session", established, CONVERGE, machines=PEERS)
    print("[test] the session is up on both ends")

    await vms.until("the other LAN over BGP", learned, CONVERGE, machines=LANS)
    print("[test] each router learned the other's LAN over BGP")

    for src, dst in ((a, "10.2.0.2"), (b, "10.1.0.2")):
        out = await src.succeed(f"ping -c 3 -W 2 {dst}")
        if "ttl=62" not in out:
            raise AssertionError(f"{src.name} reached {dst} other than through both routers:\n{out}")
    print("[test] a and b reach each other through both routers")
