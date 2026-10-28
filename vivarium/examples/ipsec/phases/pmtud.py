"""What path MTU discovery does around the tunnel, seen on the wire.

- DF is copied from the inner header to the outer one (strongSwan
  `copy_df`, on by default): a DF ping leaves west as ESP with DF, a
  ping without DF as ESP without it.
- The sender caches what the gateway told it: after one refused probe
  alice's kernel holds mtu 1446 towards bob, carol's 1400 towards dave.
- Where a packet without DF that does not fit is fragmented depends on
  the kind of tunnel. Policy-based, with no tunnel device: encrypted
  first, then fragmented, one ESP packet in two outer IP fragments that
  the far gateway must reassemble before it can decrypt. Route-based:
  the XFRM interface's MTU fragments the inner packet, and each fragment
  is its own ESP packet. AWS recommends the second.
"""

import re

import anyio
from lab import capture, captured_during, esp_inner_mtu, lab, ping, route_mtu
from vivarium_runner import Machine, Machines


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    west, alice, carol = vms.west, vms.alice, vms.carol
    bob, dave = cfg["hosts"]["bob"]["address"], cfg["hosts"]["dave"]["address"]
    east = cfg["policy"]["eastAddress"]
    westward = f"src {cfg['westAddress']} and dst {east}"

    for df, flag, want in (("do", "[DF]", "with DF"), ("dont", "[none]", "without DF")):
        rc, out = await captured_during(
            west, "internet", f"esp and {westward}", 1, lambda df=df: alice.succeed(ping(bob, count=1, extra=f"-M {df}")), "-v"
        )
        if rc != 0 or f"flags {flag}" not in out:
            raise AssertionError(f"ping -M {df}: outer ESP header should be {want}:\n{out}")
    print("[test] DF copied: ping -M do leaves west as ESP with DF, ping -M dont as ESP without it")

    for sender, target, expected in ((alice, bob, esp_inner_mtu(1500)), (carol, dave, cfg["tunnelMtu"])):
        await sender.succeed("ip route flush cache")
        await sender.execute(ping(target, count=1, extra="-M do -s 1472"))
        cached = await route_mtu(sender, target)
        if cached != expected:
            raise AssertionError(f"{sender.name}: cached path MTU to {target} is {cached}, want {expected}")
        print(f"[test] {sender.name} caches mtu {cached} towards {target} after one refused probe")

    async def oversized() -> None:
        await alice.succeed("ip route flush cache")
        await alice.succeed(ping(bob, count=1, extra="-M dont -s 1472"))

    # ESP, or any IP fragment: an outer fragment past the first has no ESP
    # header to match. Not plain `westward`: DPD sends IKE every 2 s.
    rc, out = await captured_during(
        west, "internet", f"{westward} and (esp or ip[6:2] & 0x3fff != 0)", 2, oversized, "-v"
    )
    # Measured: Linux encrypts first. The 1500-byte packet becomes one
    # 1556-byte ESP packet (20 + 8 + 8 + 1500 + 2 padding + 2 + 16), sent as
    # outer fragments of 1500 and 76 bytes; east reassembles before it can
    # decrypt. AWS recommends the other way round.
    headers = [line for line in out.splitlines() if "proto ESP" in line]
    lengths = [int(n) for line in headers for n in re.findall(r"length (\d+)\)", line)]
    first_is_fragment = bool(headers) and "flags [+]" in headers[0] and "offset 0," in headers[0]
    if rc not in (0, 124) or len(headers) != 2 or not first_is_fragment or sum(lengths) - 20 != 1556:
        raise AssertionError(f"expected one 1556-byte ESP packet in two outer fragments:\n{out}")
    print(f"[test] policy-based, 1500 bytes without DF: encrypted first, then fragmented: outer {lengths[0]} + {lengths[1]} = 1556 ESP")

    # Not a count of ESP packets on the internet: BGP keepalives cross the
    # same tunnels as 128-byte ESP packets. The proof is at both ends: the
    # inner packet arrives on east's XFRM interfaces as IP fragments, and
    # no outer fragment crosses the internet.
    carol_address = cfg["hosts"]["carol"]["address"]
    endpoints = " or ".join(f"dst {t['eastAddress']}" for t in cfg["tunnels"].values())
    seen: dict[str, tuple[int, str]] = {}

    async def watch(name: str, vm: Machine, interface: str, expression: str, count: int) -> None:
        seen[name] = await capture(vm, interface, expression, count, 10, "-v")

    async with anyio.create_task_group() as tg:
        tg.start_soon(watch, "inner", vms.east, "any", f"src {carol_address} and ip[6:2] & 0x3fff != 0", 2)
        tg.start_soon(watch, "outer", west, "internet", f"src {cfg['westAddress']} and ({endpoints}) and ip[6:2] & 0x3fff != 0", 1)
        await anyio.sleep(2)
        await carol.succeed("ip route flush cache")
        await carol.succeed(ping(dave, count=1, extra="-M dont -s 1472"))
    rc, inner = seen["inner"]
    interfaces = set(re.findall(r"\b(ipsec\d) In\b", inner))
    if rc != 0 or not interfaces:
        raise AssertionError(f"the inner packet did not reach east's XFRM interfaces as two fragments:\n{inner}")
    if seen["outer"][0] != 124:
        raise AssertionError(f"an outer IP fragment crossed the internet:\n{seen['outer'][1]}")
    lengths = re.findall(r"length (\d+)\)", inner)
    print(
        f"[test] route-based, 1500 bytes without DF: fragmented to the {cfg['tunnelMtu']} MTU first, "
        f"inner fragments of {' and '.join(lengths)} on {', '.join(sorted(interfaces))}, no outer fragment"
    )
