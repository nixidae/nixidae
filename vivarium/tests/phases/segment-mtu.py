"""Each interface has its segment's MTU, and a frame one byte over it does not leave."""

from vivarium_runner import Machines

NARROW = 1400
WIDE = 65000
"""`vivarium.mtu`'s default: `wide` declares nothing."""

ICMP = 28
"""IPv4 and ICMP headers: `ping -s` names the payload."""


async def test(vms: Machines) -> None:
    for vm in vms.values():
        for interface, want in (("narrow", NARROW), ("wide", WIDE)):
            mtu = int(await vm.succeed(f"cat /sys/class/net/{interface}/mtu"))
            if mtu != want:
                raise AssertionError(f"{vm.name}: {interface} has MTU {mtu}, want {want}")
    print(f"[test] narrow is {NARROW} and wide is {WIDE} on every guest")

    a = vms.a
    await a.wait_until_succeeds(f"ping -c 1 -W 2 -M do -s {NARROW - ICMP} 10.81.1.2", 60)
    rc, out = await a.execute(f"ping -c 1 -W 2 -M do -s {NARROW - ICMP + 1} 10.81.1.2")
    if rc == 0:
        raise AssertionError(f"a {NARROW + 1}-byte packet left on narrow:\n{out}")
    print(f"[test] narrow carries {NARROW} bytes through the hub, and refuses {NARROW + 1}")

    await a.wait_until_succeeds(f"ping -c 1 -W 2 -M do -s {9000 - ICMP} 10.81.2.2", 60)
    print("[test] wide carries 9000 bytes")
