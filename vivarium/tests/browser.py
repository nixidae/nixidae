"""checks.browser `serve`: https://web.test/ with a certificate the run's intermediate signs.

The leaf and the intermediate go to the guest through its `/artifacts`.
"""

from __future__ import annotations

import datetime

from vivarium_ca import authority
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    web = vms["web"]
    if web.artifacts is None:
        raise AssertionError("web has no /artifacts to take its certificate through")
    if vms.trust is None:
        raise AssertionError("the run has no lab CA")
    issuer = await vms.trust.issuer()
    intermediate = authority.load(issuer.certificate, issuer.key)
    leaf = authority.leaf(intermediate, ["web.test"], datetime.datetime.now(datetime.UTC))
    tls = web.artifacts / "tls"
    tls.mkdir(parents=True, exist_ok=True)
    (tls / "cert.pem").write_text(leaf.certificate_pem() + issuer.certificate)
    (tls / "key.pem").write_text(leaf.key_pem())
    await web.succeed("systemctl start web-https")
    await web.wait_until_succeeds("ss -Htln sport = :443 | grep -q .")
    web.say("https://web.test/ serves a leaf of the run's intermediate")
