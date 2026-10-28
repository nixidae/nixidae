"""Tunnel 1 dies on the wire; BGP and DPD move everything to tunnel 2.

east drops every packet to and from its tunnel 1 endpoint, as a failed
path would. The interfaces stay up, so nothing local notices: west's
BGP hold timer (3 s) withdraws the route, and its DPD (up to 2 s idle,
then three retransmits, dead about 5.4 s after the first request)
declares the IKE_SA dead and restarts it.
While the path is down west keeps trying; when it is back, tunnel 1
comes up by itself, BGP returns and the route is ECMP again.

Not `ip link set down`: under UML a link once down never comes back
(the vector driver closes the segment fd).
"""

import anyio
from lab import bgp_peers, bgp_up, established, lab, loss, nexthops, ping, sas, until
from vivarium_runner import Machine, Machines


async def cut(vm: Machine, address: str) -> None:
    await vm.succeed(
        "nft add table inet cut; "
        "nft add chain inet cut input '{ type filter hook input priority -10; }'; "
        "nft add chain inet cut output '{ type filter hook output priority -10; }'; "
        f"nft add rule inet cut input ip daddr {address} drop; "
        f"nft add rule inet cut output ip saddr {address} drop"
    )


async def mend(vm: Machine) -> None:
    await vm.succeed("nft delete table inet cut")


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    west, east, carol = vms.west, vms.east, vms.carol
    tunnels = cfg["tunnels"]
    dave = cfg["hosts"]["dave"]["address"]
    prefix = cfg["routed"]["east"]
    first, second = tunnels["1"]["east"], tunnels["2"]["east"]

    outcome: dict[str, str] = {}

    async def load() -> None:
        _rc, outcome["ping"] = await carol.execute(ping(dave, count=150, extra="-i 0.1"), timeout=40)

    async with anyio.create_task_group() as tg:
        tg.start_soon(load)
        await anyio.sleep(1)
        await cut(east, tunnels["1"]["eastAddress"])
        cut_at = anyio.current_time()

        async def withdrawn():
            peers = await bgp_peers(west)
            route = await west.succeed(f"ip route show {prefix}")
            if peers.get(first, {}).get("state") != "Established" and nexthops(route) == ["ipsec2"]:
                return route
            return None

        await until(west, "the route to fall back to ipsec2", withdrawn, 15)
        print(f"[test] tunnel 1 cut: BGP to {first} down, {prefix} only via ipsec2 after {anyio.current_time() - cut_at:.1f}s")

    sent, received = loss(outcome["ping"])
    # Measured: 22 of 150 lost, 2.2 s at one ping per 100 ms, under the
    # 3 s hold timer.
    if sent - received > 40:
        raise AssertionError(f"{sent - received} of {sent} pings lost: the route took more than 4 s to move")
    print(f"[test] carol -> dave across the failure: {received}/{sent} answered ({sent - received} lost)")
    await carol.succeed(ping(dave))

    async def dead():
        sa = (await sas(west)).get("tunnel1")
        return True if not sa or sa.get("state") != "ESTABLISHED" else None

    await until(west, "DPD to declare tunnel1 dead", dead, 30)
    if (await bgp_peers(west)).get(second, {}).get("state") != "Established":
        raise AssertionError("BGP over tunnel 2 went down with tunnel 1")
    print("[test] DPD took tunnel1's IKE_SA down; tunnel 2 and its BGP session untouched")

    await mend(east)
    await established(west, "tunnel1")
    await bgp_up(west, [first, second])

    async def ecmp():
        route = await west.succeed(f"ip route show {prefix}")
        return route if sorted(nexthops(route)) == ["ipsec1", "ipsec2"] else None

    await until(west, "ECMP over both tunnels again", ecmp)
    await carol.succeed(ping(dave))
    print("[test] path back: tunnel1 re-established by west, BGP up, ECMP over both tunnels again")
