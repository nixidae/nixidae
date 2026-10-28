"""Two sites with the same subnet, joined by 1:1 prefix NAT on each gateway.

erin behind west and frank behind east both are 10.50.0.10/24. Each
gateway maps its 10.50.0.0/24 to a range of its own, one per path, so a
host reaches the far one by the far mapped address and is seen there by
its own mapped address. Host bits stay as they are: .10 is .10 on every
side.

- Without BGP: a second CHILD_SA, `netmap`, under the policy IKE_SA,
  with the two policy-mapped ranges as selectors.
- With BGP: each gateway announces its routed-mapped range over both
  route-based tunnels. 10.50.0.0/24 is never announced.
"""

import re

import anyio
from lab import bgp_up, capture, child, established, lab, nexthops, ping
from vivarium_runner import Machine, Machines

PORT = 7000


def host(cidr: str, address: str) -> str:
    """*address*'s host bits in the /24 *cidr*."""
    return ".".join([*cidr.split("/")[0].split(".")[:3], address.split(".")[3]])


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    nm, hosts = cfg["netmap"], cfg["hosts"]
    west, east, erin, frank = vms.west, vms.east, vms.erin, vms.frank
    address = hosts["erin"]["address"]

    if hosts["frank"]["address"] != address:
        raise AssertionError(f"erin and frank must collide: {address} and {hosts['frank']['address']}")
    for vm in (erin, frank):
        if f"inet {address}/24" not in await vm.succeed("ip -4 addr show lan"):
            raise AssertionError(f"{vm.name} does not hold {address}")
    print(f"[test] erin behind west and frank behind east are both {address}")

    # On demand, as in the policy phase. The out policy matches only after
    # SNAT, and that second lookup is what raises the acquire.
    await erin.execute(ping(host(nm["east"]["policy"], address), count=1))
    sa = await established(west, "policy", "netmap")
    esp = child(sa, "netmap")
    assert esp is not None
    if esp["local-ts"] != [nm["west"]["policy"]] or esp["remote-ts"] != [nm["east"]["policy"]]:
        raise AssertionError(f"CHILD_SA netmap selectors: {esp}")
    print(f"[test] CHILD_SA netmap under IKE_SA policy: {nm['west']['policy']} === {nm['east']['policy']}")

    peers = [t["east"] for t in cfg["tunnels"].values()]
    await bgp_up(west, peers)
    for vm, other in ((west, "east"), (east, "west")):
        prefix = nm[other]["routed"]
        route = await vm.succeed(f"ip route show {prefix}")
        if sorted(nexthops(route)) != ["ipsec1", "ipsec2"]:
            raise AssertionError(f"{vm.name}: {prefix} is not ECMP over ipsec1 and ipsec2:\n{route}")
        if (await vm.succeed(f"ip route show {nm['inside']} proto bgp")).strip():
            raise AssertionError(f"{vm.name} learned {nm['inside']} over BGP")
    print(
        f"[test] BGP: {nm['east']['routed']} on west and {nm['west']['routed']} on east, ECMP; {nm['inside']} never announced"
    )

    for vm in (erin, frank):
        await vm.succeed(
            f"systemd-run --collect --unit whoami socat -U TCP-LISTEN:{PORT},reuseaddr,fork OPEN:/proc/sys/kernel/hostname"
        )

    # Not a count of ESP on the internet: BGP keepalives are ESP too. The
    # far gateway shows where the decrypted packet came in, before DNAT:
    # on the internet interface for the policy CHILD_SA, on an XFRM
    # interface for the routed path.
    arrives = {"policy": r"\binternet In\b", "routed": r"\bipsec\d In\b"}
    for path in ("policy", "routed"):
        for source, target, near, far in ((erin, frank, "west", "east"), (frank, erin, "east", "west")):
            seen_as = host(nm[near][path], address)
            reach = host(nm[far][path], address)
            results: dict[str, tuple[int, str]] = {}

            # A leak check waits out its whole window; 5 s covers the ping.
            async def watch(
                name: str,
                vm: Machine,
                interface: str,
                expression: str,
                count: int,
                flags: str = "",
                seconds: float = 12,
                results: dict[str, tuple[int, str]] = results,
            ) -> None:
                results[name] = await capture(vm, interface, expression, count, seconds, flags)

            async with anyio.create_task_group() as tg:
                tg.start_soon(watch, "target", target, "lan", f"icmp and src {seen_as} and dst {address}", 3)  # type: ignore[arg-type]  # zuban-bug: start_soon counts defaulted parameters
                tg.start_soon(watch, "decrypted", vms[far], "any", f"icmp and src {seen_as} and dst {reach}", 3)  # type: ignore[arg-type]  # zuban-bug: start_soon counts defaulted parameters
                tg.start_soon(watch, "plain-west", west, "internet", "icmp", 1, "-Q out", 5)  # type: ignore[arg-type]  # zuban-bug: start_soon counts defaulted parameters
                tg.start_soon(watch, "plain-east", east, "internet", "icmp", 1, "-Q out", 5)  # type: ignore[arg-type]  # zuban-bug: start_soon counts defaulted parameters
                await anyio.sleep(2)
                await source.succeed(ping(reach))
            if results["target"][0] != 0:
                raise AssertionError(f"{target.name} did not see {seen_as} -> {address}:\n{results['target'][1]}")
            rc, out = results["decrypted"]
            if rc != 0 or not re.search(arrives[path], out):
                raise AssertionError(f"{far}: {seen_as} -> {reach} did not arrive from the {path} path:\n{out}")
            for side in ("plain-west", "plain-east"):
                if results[side][0] != 124:
                    raise AssertionError(f"{side}: ICMP left for the internet in the clear:\n{results[side][1]}")
            name = await source.succeed(f"timeout 5 socat -u TCP:{reach}:{PORT},connect-timeout=3 STDOUT")
            if name.strip() != target.name:
                raise AssertionError(f"{source.name} -> {reach}:{PORT} answered {name!r}, want {target.name}")
            via = re.search(arrives[path], out)
            assert via is not None
            print(
                f"[test] {path}: {source.name} {address} -> {reach}, decrypted on {far}'s {via.group(0).split()[0]}, "
                f"{target.name} sees {seen_as} -> {address}; TCP answers {name.strip()}"
            )

    after = child(await established(west, "policy", "netmap"), "netmap")
    assert after is not None
    if int(after["packets-out"]) < 3 or int(after["packets-in"]) < 3:
        raise AssertionError(f"CHILD_SA netmap counters did not move: {after}")
    print(f"[test] CHILD_SA netmap counters: {after['packets-out']} out, {after['packets-in']} in")

    # The original direction holds the inside address, the reply direction
    # the mapped one it was rewritten to.
    near, far = host(nm["west"]["routed"], address), host(nm["east"]["routed"], address)
    await erin.succeed(f"timeout 5 socat -u TCP:{far}:{PORT} STDOUT")
    table = await west.succeed(f"conntrack -L -p tcp --dport {PORT} 2>/dev/null")
    want = rf"src={re.escape(address)} dst={re.escape(far)} .* src={re.escape(far)} dst={re.escape(near)} "
    if not re.search(want, table):
        raise AssertionError(f"west's conntrack does not show the translation:\n{table}")
    print(f"[test] west conntrack: {address} -> {far} out, {far} -> {near} back")

    for vm in (erin, frank):
        await vm.succeed("systemctl stop whoami")
