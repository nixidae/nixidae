"""`vivarium.recipes.lab`: each named guest holds its address, and each guest
with `vivarium.lab.tls.enable` gets a certificate.

The run's intermediate signs a leaf for the guest's lab names. The leaf
and its key reach the guest through the agent, as variables of one
command, never through `/artifacts`, which the host keeps. The key goes in
last, readable by `vivarium.lab.tls.group`: the guest's path unit waits for
it, then starts `vivarium.lab.tls.units`.
nixidae/nixidae#1669.
"""

from __future__ import annotations

import datetime

from vivarium_ca import authority
from vivarium_runner import Machines

INSTALL = """
set -eu
umask 022
mkdir -p "$DIRECTORY"
printf '%s' "$CERTIFICATE" > "$DIRECTORY/cert.pem.new"
mv "$DIRECTORY/cert.pem.new" "$DIRECTORY/cert.pem"
umask 077
printf '%s' "$KEY" > "$DIRECTORY/key.pem.new"
chgrp "$GROUP" "$DIRECTORY/key.pem.new"
chmod 0640 "$DIRECTORY/key.pem.new"
mv "$DIRECTORY/key.pem.new" "$DIRECTORY/key.pem"
"""


async def test(vms: Machines) -> None:
    # A guest is running before the router leases it its address; a name
    # that leads nowhere yet is refused (measured: curl's 7, in 3 ms).
    addresses: dict[str, str] = vms.settings["lab"]["addresses"]
    for guest, address in addresses.items():
        await vms[guest].wait_until_succeeds(f"ip -4 -o address show | grep -qF ' {address}/'", timeout=60)
    wanted: dict[str, dict[str, object]] = vms.settings["lab"]["tls"]  # JSON from the recipe
    if not wanted:
        return
    if vms.trust is None:
        raise AssertionError("the run has no lab CA")
    issuer = await vms.trust.issuer()
    intermediate = authority.load(issuer.certificate, issuer.key)
    now = datetime.datetime.now(datetime.UTC)
    for guest, want in wanted.items():
        listed = want["names"]
        if not isinstance(listed, list):
            raise TypeError(f"settings.lab.tls.{guest}.names is {listed!r}, not a list")
        names = [str(name) for name in listed]
        leaf = authority.leaf(intermediate, names, now)
        done = await vms[guest].execute(
            INSTALL,
            label="install the lab certificate",
            env={
                "DIRECTORY": str(want["directory"]),
                "GROUP": str(want["group"]),
                "CERTIFICATE": leaf.certificate_pem() + issuer.certificate,
                "KEY": leaf.key_pem(),
            },
        )
        if done.returncode != 0:
            raise AssertionError(f"{guest}: installing the certificate failed: {done.stderr}")
        vms[guest].say(f"a certificate for {', '.join(names)} from the run's lab CA")
