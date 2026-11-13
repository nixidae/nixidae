"""The underlay: eBGP unnumbered over IPv6 link-local, IPv4 loopbacks.

Every switch's loopback reaches every other, sourced from its own, but
for spine to spine: the spines share one ASN, so each refuses the
other's loopback as an AS-path loop, as the reference design intends. A
leaf reaches the other leaf over both spines at once. No switch takes a
default route from a fabric peer's router advertisements.
"""

from fabric import CONVERGE, bgp_down, bgp_up, loopbacks, nexthops, ping, switches
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    fabric = switches(vms)
    await vms.until("BGP established, IPv4 and EVPN", bgp_up, CONVERGE, machines=fabric, evidence=bgp_down)
    print(f"[test] BGP established on {', '.join(fabric)}, IPv4 and EVPN")

    addresses = loopbacks(vms)
    spines = set(vms.settings["clos"]["spines"])
    for name, vm in fabric.items():
        for other, address in addresses.items():
            if other != name and not {name, other} <= spines:
                await vm.wait_until_succeeds(ping(address, addresses[name]), CONVERGE)
    print("[test] every loopback reaches every other, spine to spine aside")

    leaves = list(vms.settings["clos"]["leaves"])
    first, last = leaves[0], leaves[-1]
    route = await vms[first].wait_until_succeeds(f"ip route show {addresses[last]} | grep -c nexthop", CONVERGE)
    paths = nexthops(await vms[first].succeed(f"ip route show {addresses[last]}"))
    if paths != len(vms.settings["clos"]["spines"]):
        raise AssertionError(f"{first} reaches {last} over {paths} paths: {route}")
    print(f"[test] {first} reaches {last} over {paths} spines at once")

    for name, vm in fabric.items():
        defaults = await vm.succeed("ip -6 route show default; ip -4 route show default")
        stray = [line for line in defaults.splitlines() if line and "dev vec0" not in line]
        if stray:
            raise AssertionError(f"{name} took a default route from the fabric: {stray}")
    print("[test] no switch routes its default through the fabric")
