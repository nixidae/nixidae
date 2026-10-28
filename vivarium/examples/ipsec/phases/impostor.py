"""mallory claims to be west four ways, and east lets none of them in.

east's policy connection takes any outside address, so the certificate
is all that stands between mallory and alice's LAN.

  revoked  west's identity and a certificate the CA revoked for key
           compromise: east's strict revocation check refuses it.
  rogue    west's identity, signed by a CA east does not trust.
  weak     AES-128-CBC/SHA-1/MODP-1024: refused at IKE_SA_INIT, before
           any certificate is looked at.
  spoof    plaintext from bob's address, routed at west: the kernel's
           fwd policy wants ESP for that pair, so it never reaches alice.

None of it disturbs the real IKE_SA: west's SPIs are the same at the
end, and alice still reaches bob.
"""

import anyio
from lab import capture, epoch, established, journal_since, lab, ping, until, xfrm_stats
from vivarium_runner import Machines

# What east logs as the reason, and the notify it answers with.
ATTEMPTS = {
    "revoked": ("certificate was revoked", "N(AUTH_FAILED)"),
    "rogue": ('issuer is "C=SE, O=vivarium, CN=rogue CA"', "N(AUTH_FAILED)"),
    "weak": ("received proposals: IKE:AES_CBC_128/HMAC_SHA1_96/PRF_HMAC_SHA1/MODP_1024", "N(NO_PROP)"),
}


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    west, east, mallory, alice = vms.west, vms.east, vms.mallory, vms.alice
    before = await established(west, "policy")

    for name, (needle, notify) in ATTEMPTS.items():
        since = await epoch(east)
        out = await mallory.fail(f"swanctl --initiate --ike {name} --child steal --timeout 15", timeout=30)
        # charon's lines reach the journal after mallory has its answer.
        async def logged(since: float = since, needle: str = needle, notify: str = notify) -> str | None:
            log = await journal_since(east, "strongswan-swanctl.service", since)
            return log if needle in log and notify in log else None

        try:
            log = await until(east, f"east to log the {name} refusal", logged, 15)
        except AssertionError:
            log = await journal_since(east, "strongswan-swanctl.service", since)
            raise AssertionError(
                f"{name}: east did not refuse with {needle!r} and {notify}.\nmallory:\n{out}\neast:\n{log}"
            ) from None
        reason = next(line for line in log.splitlines() if needle in line)
        print(f"[test] {name}: east answers {notify}: {reason.strip()}")

    mallory_sas = await mallory.succeed("swanctl --list-sas")
    if "ESTABLISHED" in mallory_sas:
        raise AssertionError(f"mallory holds an IKE_SA:\n{mallory_sas}")

    bob, alice_address = cfg["hosts"]["bob"]["address"], cfg["hosts"]["alice"]["address"]
    await mallory.succeed(
        f"ip address replace {bob}/32 dev lo && ip route replace {cfg['policy']['west']} via {cfg['westAddress']} src {bob}"
    )
    stats_before = await xfrm_stats(west)
    seen: dict[str, tuple[int, str]] = {}

    async def watch() -> None:
        seen["alice"] = await capture(alice, "lan", f"icmp and src {bob}", 1, 10)

    async with anyio.create_task_group() as tg:
        tg.start_soon(watch)
        await anyio.sleep(2)
        await mallory.fail(ping(alice_address, count=3, extra=f"-I {bob}"))
    if seen["alice"][0] != 124:
        raise AssertionError(f"spoofed plaintext reached alice:\n{seen['alice'][1]}")
    stats_after = await xfrm_stats(west)
    # A packet with no secpath that matches a policy whose template
    # wants ESP is counted as a template mismatch, once per packet.
    moved = {k: stats_after[k] - v for k, v in stats_before.items() if stats_after[k] != v}
    if moved != {"XfrmInTmplMismatch": 3}:
        raise AssertionError(f"west's XFRM counters for 3 spoofed packets: {moved}")
    print(f"[test] spoof: plaintext {bob} -> {alice_address} dropped by west's fwd policy: {moved}")

    after = await established(west, "policy")
    if (after["initiator-spi"], after["responder-spi"]) != (before["initiator-spi"], before["responder-spi"]):
        raise AssertionError("west's IKE_SA changed while mallory was at it")
    await alice.succeed(ping(bob))
    print("[test] west's IKE_SA untouched, alice still reaches bob")
