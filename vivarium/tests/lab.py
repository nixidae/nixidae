"""checks.lab: the guests' view of `vivarium.recipes.lab`, and units that come back.

`client` reaches `web` by its lab names through `/etc/hosts`, with the
run's lab root. Inside the lab nothing follows SRV, so the api is
`:8443`. Then nginx comes back by itself twice: from the path unit, as at
a reboot with the key on disk, and through `switch-to-configuration`.
Negative control: without the lab root, `client`'s curl refuses web.
"""

from __future__ import annotations

from vivarium import trust
from vivarium_runner import Machine, Machines

PAGES = {
    "https://web.lab.test/": "hello from web.lab.test",
    "https://api.lab.test:8443/": "hello from the api on 8443",
}


async def fetch(client: Machine, url: str, root: str | None) -> tuple[int, str]:
    """curl in *client*, trusting *root* alone, or the guest's own roots when None."""
    curl = f"curl --silent --show-error --max-time 10 {url}"
    if root is None:
        done = await client.execute(curl)
    else:
        command = f"printf '%s' \"$ROOT\" > /tmp/lab-root.pem && {curl} --cacert /tmp/lab-root.pem"
        done = await client.execute(command, env={"ROOT": root})
    return done.returncode, (done.stdout or done.stderr).strip()


async def pages(client: Machine, root: str) -> None:
    for url, said in PAGES.items():
        rc, out = await fetch(client, url, root)
        if rc != 0 or out != said:
            raise AssertionError(f"client, {url}: {out}")


async def test(vms: Machines) -> None:
    web, client = vms["web"], vms["client"]
    root = (vms.access / trust.CERTIFICATE).read_text()
    await web.wait_for_unit("nginx")

    hosts = (await client.succeed("getent hosts web.lab.test api.lab.test")).stdout
    if len(hosts.splitlines()) != 2:
        raise AssertionError(f"client's /etc/hosts lacks a lab name:\n{hosts}")
    await pages(client, root)
    client.say("reaches web.lab.test and api.lab.test:8443 by name, trusting the lab root")

    rc, out = await fetch(client, "https://web.lab.test/", None)
    if rc == 0:
        raise AssertionError(f"client without the lab root trusted web: {out}")
    client.say(f"without the lab root: {out.splitlines()[0]}")

    # As at a reboot: the key is on disk, and the path unit starts the rest.
    await web.succeed("systemctl stop nginx vivarium-tls.target vivarium-tls.path")
    await web.fail("systemctl is-active --quiet nginx")
    await web.succeed("systemctl start vivarium-tls.path")
    await web.wait_for_unit("nginx")
    await pages(client, root)
    web.say("nginx is back from the path unit, with the key on disk")

    await web.succeed("/run/current-system/bin/switch-to-configuration test")
    await web.wait_for_unit("nginx")
    await pages(client, root)
    web.say("nginx runs on after switch-to-configuration")
