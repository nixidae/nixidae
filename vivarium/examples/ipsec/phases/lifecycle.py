"""What a certificate's lifetime does to a running tunnel.

  renew    west loads a renewed certificate and key and reauthenticates
           under load. IKEv2 reauthentication is make-before-break.
  expired  west loads a certificate that expired in 2021. east refuses
           the reauthentication, yet the old IKE_SA stays up and traffic
           flows: swanctl even reports the rekey as completed. The outage
           comes when the IKE_SA must start from nothing, here a
           terminate, in life a reboot or a DPD timeout.
  stale    east loads a CRL whose nextUpdate passed in 2021. With
           `revocation = strict` that refuses a peer whose own
           certificate is good, with the same delayed outage. `pki
           --signcrl` defaults to 15 days, so a CRL nobody re-signs
           breaks every tunnel at its next restart.

west ends on the renewed certificate, serial 04.

Measured: 3 reauthentications of 22 lost one ping each, with no XFRM
error counter moving; a rekey lost none in any try.
"""

import re

import anyio
from lab import epoch, established, eventually, journal_since, lab, loss, ping, until
from vivarium_runner import Machine, Machines

SPARE = "/etc/ipsec-lab"
SWANCTL = "/etc/swanctl"
UNIT = "strongswan-swanctl.service"


async def swap(vm: Machine, files: dict[str, str]) -> None:
    """Install spare files over the loaded ones and reload every credential."""
    installs = " && ".join(
        f"install -m {'0400' if target.startswith('private/') else '0444'} {source} {SWANCTL}/{target}"
        for target, source in files.items()
    )
    await vm.succeed(f"{installs} && swanctl --load-creds --clear --noprompt")


def ike_spis(sa: dict) -> tuple[str, str]:
    return sa["initiator-spi"], sa["responder-spi"]


async def refused(east: Machine, since: float, needle: str) -> str:
    async def logged() -> str | None:
        log = await journal_since(east, UNIT, since)
        return log if needle in log and "N(AUTH_FAILED)" in log else None

    log = await until(east, f"east to refuse with {needle!r}", logged, 15)
    return next(line.strip() for line in log.splitlines() if needle in line)


async def delayed_outage(west: Machine, east: Machine, alice: Machine, bob: str, needle: str) -> str:
    """Reauthenticate, see east refuse while traffic flows, then terminate."""
    before = await established(west, "policy")
    since = await epoch(east)
    await west.succeed("swanctl --rekey --ike policy --reauth", timeout=30)
    reason = await refused(east, since, needle)
    after = await established(west, "policy")
    if ike_spis(after) != ike_spis(before):
        raise AssertionError("a refused reauthentication replaced the IKE_SA")
    await alice.succeed(ping(bob, count=2))

    await west.succeed("swanctl --terminate --ike policy --timeout 10", timeout=30)
    await alice.fail(ping(bob, count=3))
    return reason


async def test(vms: Machines) -> None:
    cfg = lab(vms)
    west, east, alice = vms.west, vms.east, vms.alice
    bob = cfg["hosts"]["bob"]["address"]
    await east.succeed(f"mkdir -p /root/orig && cp {SWANCTL}/x509crl/ca.crl /root/orig/")

    before = await established(west, "policy")
    outcome: dict[str, str] = {}

    async def load() -> None:
        outcome["ping"] = await alice.succeed(ping(bob, count=60, extra="-i 0.1"), timeout=30)

    async with anyio.create_task_group() as tg:
        tg.start_soon(load)
        await anyio.sleep(1)
        await swap(west, {"x509/west.pem": f"{SPARE}/renewed.pem", "private/west.key": f"{SPARE}/renewed.key"})
        await west.succeed("swanctl --rekey --ike policy --reauth", timeout=30)
    sent, received = loss(outcome["ping"])
    if sent - received > 1:
        raise AssertionError(f"{sent - received} of {sent} pings lost across a make-before-break reauth")
    after = await established(west, "policy")
    if ike_spis(after) == ike_spis(before):
        raise AssertionError("the reauthentication kept the old IKE_SA")
    certs = await west.succeed("swanctl --list-certs --type x509 --subject 'C=SE, O=vivarium, CN=west.vpn.test'")
    if not re.search(r"serial:\s+04\b", certs) or "has private key" not in certs:
        raise AssertionError(f"west is not on the renewed certificate:\n{certs}")
    print(f"[test] renew: west on serial 04 with a new key, new IKE_SA, {received}/{sent} pings")

    await swap(west, {"x509/west.pem": f"{SPARE}/expired.pem", "private/west.key": f"{SPARE}/expired.key"})
    reason = await delayed_outage(west, east, alice, bob, "subject certificate invalid (valid from")
    print(f"[test] expired: east refuses ({reason}); the old IKE_SA carries traffic until it is terminated")
    await swap(west, {"x509/west.pem": f"{SPARE}/renewed.pem", "private/west.key": f"{SPARE}/renewed.key"})
    await eventually(alice, ping(bob, count=1), 15)
    print("[test] expired: the renewed certificate back, the trap policy brings the tunnel up on the next packet")

    await swap(east, {"x509crl/ca.crl": f"{SPARE}/stale.crl"})
    reason = await delayed_outage(west, east, alice, bob, "RULE_CRL_VALIDATION is STALE")
    print(f"[test] stale CRL: east refuses a good certificate ({reason}); the outage again waits for a terminate")
    await swap(east, {"x509crl/ca.crl": "/root/orig/ca.crl"})
    await eventually(alice, ping(bob, count=1), 15)
    await established(west, "policy")
    print("[test] stale CRL: a current CRL back on east, the tunnel up again")
