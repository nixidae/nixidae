"""The route-based tunnels: XFRM interfaces, eBGP over each, as AWS does it.

Both tunnels carry 0.0.0.0/0 selectors and an interface ID, so the
kernel only encrypts what routing sends into ipsec1 or ipsec2. BGP over
each tunnel decides what that is: west learns 10.12.0.0/24 over both and
installs it with two nexthops. east also announces 10.99.0.0/24, which
west's inbound prefix list refuses: it reaches west and stays out of
its routing table.

Then isolation: the policy LANs and the routed LANs do not reach each
other in either direction, and nothing leaves a gateway in the clear.
"""

import anyio

from lab import bgp_up, capture, child, established, lab, nexthops, ping, vtysh
from vivarium_runner import Machine, Machines


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    west, east = vms.west, vms.east
    tunnels = cfg["tunnels"]

    for n, tunnel in tunnels.items():
        for vm in (west, east):
            sa = await established(vm, f"tunnel{n}")
            esp = child(sa, f"tunnel{n}")
            assert esp is not None
            if esp["local-ts"] != ["0.0.0.0/0"] or esp["remote-ts"] != ["0.0.0.0/0"]:
                raise AssertionError(f"{vm.name} tunnel{n} selectors: {esp}")
            if int(esp.get("if-id-in", "0"), 16) != int(n) or int(esp.get("if-id-out", "0"), 16) != int(n):
                raise AssertionError(f"{vm.name} tunnel{n} interface IDs: {esp}")
            link = await vm.succeed(f"ip -d link show ipsec{n}")
            if f"if_id 0x{int(n):x}" not in link:
                raise AssertionError(f"{vm.name} ipsec{n} is not XFRM interface {n}:\n{link}")
        print(f"[test] tunnel{n}: {cfg['westAddress']} <-> {tunnel['eastAddress']}, 0/0 selectors, if_id {n}")

    west_peers = [t["east"] for t in tunnels.values()]
    east_peers = [t["west"] for t in tunnels.values()]
    await bgp_up(west, west_peers)
    await bgp_up(east, east_peers)
    print(f"[test] eBGP AS{cfg['asn']['west']} <-> AS{cfg['asn']['east']} established over both tunnels")

    routed = cfg["routed"]
    for vm, prefix in ((west, routed["east"]), (east, routed["west"])):
        route = await vm.succeed(f"ip route show {prefix}")
        if sorted(nexthops(route)) != ["ipsec1", "ipsec2"]:
            raise AssertionError(f"{vm.name}: {prefix} is not ECMP over ipsec1 and ipsec2:\n{route}")
    print(f"[test] {routed['east']} on west and {routed['west']} on east: ECMP over ipsec1 and ipsec2")

    peer = tunnels["1"]["east"]
    advertised = await vtysh(east, f"show bgp ipv4 unicast neighbors {tunnels['1']['west']} advertised-routes")
    received = await vtysh(west, f"show bgp ipv4 unicast neighbors {peer} received-routes")
    accepted = await vtysh(west, f"show bgp ipv4 unicast neighbors {peer} routes")
    if routed["bogus"] not in advertised.get("advertisedRoutes", {}):
        raise AssertionError(f"east does not announce {routed['bogus']}: {advertised}")
    if routed["bogus"] not in received.get("receivedRoutes", {}):
        raise AssertionError(f"west never received {routed['bogus']}: {received}")
    if routed["bogus"] in accepted.get("routes", {}):
        raise AssertionError(f"west accepted {routed['bogus']}: {accepted}")
    if (await west.succeed(f"ip route show {routed['bogus']}")).strip():
        raise AssertionError(f"{routed['bogus']} reached west's routing table")
    print(f"[test] east announces {routed['bogus']}, west receives it and its prefix list drops it")

    hosts = cfg["hosts"]
    results: dict[str, tuple[int, str]] = {}

    async def watch(name: str, vm: Machine, expression: str, count: int) -> None:
        results[name] = await capture(vm, "internet", expression, count, 12)

    endpoints = " or ".join(f"host {t['eastAddress']}" for t in tunnels.values())
    async with anyio.create_task_group() as tg:
        tg.start_soon(watch, "esp", west, f"esp and ({endpoints})", 6)
        tg.start_soon(watch, "plain", west, "icmp", 1)
        await anyio.sleep(2)
        out = await vms.carol.succeed(ping(hosts["dave"]["address"]))
        print(f"[test] carol -> dave: {out.strip().splitlines()[-2]}")
    if results["esp"][0] != 0:
        raise AssertionError(f"no ESP on the tunnels while carol pinged dave:\n{results['esp'][1]}")
    if results["plain"][0] != 124:
        raise AssertionError(f"ICMP crossed the internet in the clear:\n{results['plain'][1]}")
    print("[test] on the internet: ESP to the tunnel endpoints, no ICMP")

    crossings = [("alice", "dave"), ("carol", "bob"), ("bob", "carol"), ("dave", "alice")]
    async with anyio.create_task_group() as tg:
        tg.start_soon(watch, "leak-west", west, "icmp", 1)
        tg.start_soon(watch, "leak-east", east, "icmp", 1)
        await anyio.sleep(2)
        for source, target in crossings:
            await vms[source].fail(ping(hosts[target]["address"], count=2))
    for side in ("leak-west", "leak-east"):
        if results[side][0] != 124:
            raise AssertionError(f"{side}: a crossing left in the clear:\n{results[side][1]}")
    print("[test] policy LANs and routed LANs do not reach each other, and nothing leaks in the clear")
