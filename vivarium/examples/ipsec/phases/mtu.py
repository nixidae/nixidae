"""Path MTU discovery through each kind of tunnel, to the byte.

Route-based: ipsec1 and ipsec2 have an MTU of 1400, so 1372 bytes of
ICMP payload (+ 8 ICMP + 20 IP) pass with DF set and 1373 come back as
"Frag needed, mtu = 1400" from west.

Policy-based: there is no tunnel device, so the limit is what is left of
the internet's 1500 once ESP is added: 1446 (`esp_inner_mtu`).
"""

import re

from lab import esp_inner_mtu, lab
from vivarium_runner import Machine, Machines

ICMP_IP = 28


async def probe(vm: Machine, address: str, size: int) -> tuple[int, str]:
    return await vm.execute(f"ping -n -c 2 -i 0.3 -W 2 -M do -s {size} {address}")


async def path_mtu(vm: Machine, address: str, expected: int, via: str) -> None:
    rc, out = await probe(vm, address, expected - ICMP_IP)
    if rc != 0:
        # The first probe can teach the sender the MTU and be lost itself.
        rc, out = await probe(vm, address, expected - ICMP_IP)
    if rc != 0:
        raise AssertionError(f"{vm.name}: {expected} bytes to {address} with DF failed:\n{out}")
    rc, out = await probe(vm, address, expected - ICMP_IP + 1)
    reported = [int(m) for m in re.findall(r"mtu ?= ?(\d+)", out)]
    if rc == 0 or not reported or reported[0] != expected:
        raise AssertionError(f"{vm.name}: {expected + 1} bytes to {address} should report mtu {expected}:\n{out}")
    print(
        f"[test] {vm.name} -> {address} via {via}: {expected} bytes pass with DF, {expected + 1} get mtu = {reported[0]}"
    )


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    hosts = cfg["hosts"]
    await path_mtu(vms.carol, hosts["dave"]["address"], cfg["tunnelMtu"], "the XFRM interfaces")
    await path_mtu(vms.alice, hosts["bob"]["address"], esp_inner_mtu(1500), "the policy-based CHILD_SA")
