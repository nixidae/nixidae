"""The policy-based tunnel: one CHILD_SA whose selectors are the two LANs.

What it proves, in order: the IKE_SA and CHILD_SA use exactly the
proposals both ends were given; the kernel holds AES-GCM-256 for ESP and
the three policies (out, in, fwd) for exactly the two /24s; alice reaches
bob with her own address, and on the internet there is ESP between the
gateways and no ICMP at all.
"""

import re

import anyio

from lab import capture, child, established, lab, ping
from vivarium_runner import Machine, Machines


def expect(what: str, got: object, want: object) -> None:
    if got != want:
        raise AssertionError(f"{what}: got {got!r}, want {want!r}")


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    west, east, alice, bob = vms.west, vms.east, vms.alice, vms.bob
    bob_address = cfg["hosts"]["bob"]["address"]
    alice_address = cfg["hosts"]["alice"]["address"]

    # The CHILD_SA is on demand: west's trap policy turns alice's first
    # packet into an acquire, if the start at boot did not already
    # establish it.
    await alice.execute(ping(bob_address, count=1))
    sa = await established(west, "policy")
    for side_sa, local, remote in (
        (sa, cfg["ids"]["west"], cfg["ids"]["east"]),
        (await established(east, "policy"), cfg["ids"]["east"], cfg["ids"]["west"]),
    ):
        expect("IKE encryption", (side_sa["encr-alg"], side_sa["encr-keysize"]), ("AES_GCM_16", "256"))
        expect("IKE PRF", side_sa["prf-alg"], "PRF_HMAC_SHA2_384")
        expect("IKE DH group", side_sa["dh-group"], "ECP_384")
        expect("local identity", side_sa["local-id"], local)
        expect("remote identity", side_sa["remote-id"], remote)
    print("[test] IKE_SA policy: AES_GCM_16-256/PRF_HMAC_SHA2_384/ECP_384, west.vpn.test <-> east.vpn.test")

    esp = child(sa, "policy")
    assert esp is not None
    expect("ESP encryption", (esp["encr-alg"], esp["encr-keysize"]), ("AES_GCM_16", "256"))
    expect("mode", esp["mode"], "TUNNEL")
    expect("local selectors", esp["local-ts"], [cfg["policy"]["west"]])
    expect("remote selectors", esp["remote-ts"], [cfg["policy"]["east"]])
    print(f"[test] CHILD_SA policy: ESP AES_GCM_16-256, tunnel, {cfg['policy']['west']} === {cfg['policy']['east']}")

    # rfc4106 keys carry a 4-byte salt after the AES key: 256 + 32 bits.
    states = await west.succeed(f"ip xfrm state show spi 0x{esp['spi-out']}")
    key = re.search(r"aead rfc4106\(gcm\(aes\)\) 0x([0-9a-f]+) (\d+)", states)
    if not key or len(key.group(1)) * 4 != 288 or key.group(2) != "128":
        raise AssertionError(f"kernel SA is not AES-256-GCM with a 128-bit ICV:\n{states}")
    print("[test] kernel SA: rfc4106(gcm(aes)), 288-bit key (256 + salt), 128-bit ICV")

    policies = await west.succeed(f"ip xfrm policy show src {cfg['policy']['west']} dst {cfg['policy']['east']}")
    policies += await west.succeed(f"ip xfrm policy show src {cfg['policy']['east']} dst {cfg['policy']['west']}")
    for direction in ("out", "in", "fwd"):
        if f"dir {direction}" not in policies:
            raise AssertionError(f"no {direction} policy for the LANs:\n{policies}")
    print("[test] kernel policies: out, in and fwd for exactly the two /24s")

    results: dict[str, tuple[int, str]] = {}

    async def watch(name: str, vm: Machine, interface: str, expression: str, count: int) -> None:
        results[name] = await capture(vm, interface, expression, count, 12)

    async with anyio.create_task_group() as tg:
        tg.start_soon(watch, "esp", west, "internet", f"esp and host {cfg['policy']['eastAddress']}", 6)
        tg.start_soon(watch, "plain", west, "internet", "icmp", 1)
        tg.start_soon(watch, "bob", bob, "lan", f"icmp and src {alice_address}", 3)
        await anyio.sleep(2)
        out = await alice.succeed(ping(bob_address))
        print(f"[test] alice -> bob: {out.strip().splitlines()[-2]}")

    rc, out = results["esp"]
    if rc != 0 or out.count(" ESP(") < 6:
        raise AssertionError(f"no ESP between the gateways while alice pinged bob:\n{out}")
    rc, out = results["plain"]
    if rc != 124:
        raise AssertionError(f"ICMP crossed the internet in the clear:\n{out}")
    rc, out = results["bob"]
    if rc != 0:
        raise AssertionError(f"bob did not see alice's own address:\n{out}")
    print(
        f"[test] on the internet: ESP between {cfg['westAddress']} and {cfg['policy']['eastAddress']}, "
        f"no ICMP; bob sees {alice_address} unNATed"
    )

    after = child((await established(west, "policy")), "policy")
    assert after is not None
    if int(after["packets-out"]) < 3 or int(after["packets-in"]) < 3:
        raise AssertionError(f"CHILD_SA counters did not move: {after}")
    print(f"[test] CHILD_SA counters: {after['packets-out']} out, {after['packets-in']} in")
