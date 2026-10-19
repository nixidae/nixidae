"""Each gateway holds its own certificate and key, the lab CA and its CRL.

The CRL is what makes `revocation = strict` pass for a good peer: strict
fails a chain with no revocation information at all, so a gateway
without it would refuse west and east as surely as mallory.
"""

from lab import lab
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    ids = lab(vms)["ids"]
    for side in ("west", "east"):
        vm = vms[side]
        await vm.wait_for_unit("strongswan-swanctl.service")
        certs = await vm.succeed("swanctl --list-certs")
        for needle in (f"CN={ids[side]}", "has private key", "CN=vivarium IPsec lab CA"):
            if needle not in certs:
                raise AssertionError(f"{side}: {needle!r} not in swanctl --list-certs:\n{certs}")
        crl = await vm.succeed("swanctl --list-certs --type x509crl")
        if "key compromise" not in crl or "03" not in crl:
            raise AssertionError(f"{side}: the CRL does not revoke serial 03:\n{crl}")
        print(f"[test] {side}: own certificate and key, the lab CA, a CRL revoking serial 03")
