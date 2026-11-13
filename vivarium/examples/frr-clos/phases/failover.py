"""A leaf loses one spine, then both, then gets them back.

With one uplink the fabric carries on over the other spine. With none
the leaf is cut off. With both back it spreads over two spines again.

The runner cuts a link on the host (`vms.segment(...).cut()`), not
`ip link set down`: the link stays up and BGP finds out from its hold
timer, as in a real failure. Under UML a link once down never comes up
again: the vector driver closes the segment fd on down (measured:
"Network is unreachable" on up).
"""

from fabric import CONVERGE, established, loopbacks, nexthops, ping
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    clos = vms.settings["clos"]
    spines = list(clos["spines"])
    leaves = list(clos["leaves"])
    leaf, other = vms[leaves[0]], leaves[-1]
    addresses = loopbacks(vms)
    target, source = addresses[other], addresses[leaf.name]
    servers = clos["servers"]
    tenant = next(name for name, server in servers.items() if server["leaf"] == leaf.name)
    remote = next(server["address"] for server in servers.values() if server["leaf"] == other)
    uplinks = [vms.segment(f"{leaf.name}-{spine}") for spine in spines]

    await uplinks[0].cut()
    await leaf.wait_until_succeeds(f"test $(ip route show {target} | grep -c nexthop) = 0", CONVERGE)
    route = await leaf.succeed(f"ip route show {target}")
    if nexthops(route) != 1:
        raise AssertionError(f"{leaf.name} without {spines[0]}: {route}")
    await leaf.wait_until_succeeds(ping(target, source), CONVERGE)
    await vms[tenant].wait_until_succeeds(ping(remote), CONVERGE)
    print(f"[test] without {spines[0]}, {leaf.name} reaches {other} over {spines[1]}, and so does {tenant}")

    for uplink in uplinks[1:]:
        await uplink.cut()
    await leaf.wait_until_fails(ping(target, source), CONVERGE)
    await vms[tenant].wait_until_fails(ping(remote), CONVERGE)
    print(f"[test] with every uplink down, {leaf.name} and {tenant} reach nothing")

    for uplink in uplinks:
        await uplink.restore()
    await established(leaf)
    await leaf.wait_until_succeeds(f"test $(ip route show {target} | grep -c nexthop) = {len(spines)}", CONVERGE)
    await vms[tenant].wait_until_succeeds(ping(remote), CONVERGE)
    print(f"[test] with the uplinks back, {leaf.name} spreads over {len(spines)} spines again")
