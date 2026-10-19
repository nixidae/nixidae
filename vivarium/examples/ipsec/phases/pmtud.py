"""What path MTU discovery does around the tunnel, seen on the wire.

- DF is copied from the inner header to the outer one (strongSwan
  `copy_df`, on by default): a DF ping leaves west as ESP with DF, a
  ping without DF as ESP without it.
- The sender caches what the gateway told it: after one refused probe
  alice's kernel holds mtu 1446 towards bob, carol's 1400 towards dave.
- A packet without DF that does not fit is encrypted first and then
  fragmented: one ESP packet in two outer IP fragments, which the far
  gateway must reassemble before it can decrypt.
"""

import re

from lab import captured_during, lab, ping, route_mtu
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    west, alice, carol = vms.west, vms.alice, vms.carol
    bob, dave = cfg["hosts"]["bob"]["address"], cfg["hosts"]["dave"]["address"]
    east = cfg["policy"]["eastAddress"]
    westward = f"src {cfg['westAddress']} and dst {east}"

    for df, flag, want in (("do", "[DF]", "with DF"), ("dont", "[none]", "without DF")):
        rc, out = await captured_during(
            west, "internet", f"esp and {westward}", 1, lambda: alice.succeed(ping(bob, count=1, extra=f"-M {df}")), "-v"
        )
        if rc != 0 or f"flags {flag}" not in out:
            raise AssertionError(f"ping -M {df}: outer ESP header should be {want}:\n{out}")
    print("[test] DF copied: ping -M do leaves west as ESP with DF, ping -M dont as ESP without it")

    for sender, target, expected in ((alice, bob, 1446), (carol, dave, cfg["tunnelMtu"])):
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
    print(f"[test] without DF, 1500 bytes through the 1446 path: encrypted first, then fragmented: outer {lengths[0]} + {lengths[1]} = 1556 ESP")
