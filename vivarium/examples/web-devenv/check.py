"""What the README does in `vivarium shell`, from the host.

curl runs on the host with the variables `vivarium shell` sets
(`exports.shell`): the lab's proxy, and the lab root in `SSL_CERT_FILE`.
Negative controls: the public roots alone refuse the lab's certificate,
and a name the lab does not have gets no answer.
"""

from __future__ import annotations

import os

import anyio
from vivarium import exports
from vivarium_runner import Machines

PAGES = {
    "https://web.lab.test/": "hello from web.lab.test",
    "https://api.lab.test/": "hello from the api on 8443",
}


async def curl(vms: Machines, url: str, env: dict[str, str]) -> tuple[bool, str]:
    done = await anyio.run_process(
        [vms.settings["curl"], "--silent", "--show-error", "--max-time", "20", url], env=env, check=False
    )
    return done.returncode == 0, (done.stdout or done.stderr).decode().strip()


async def test(vms: Machines) -> None:
    if vms.exports is None:
        raise AssertionError("the run exports nothing")
    shell = os.environ | exports.shell(vms.exports.values, environment=None, out=vms.access.parent)
    web = vms["web"]

    # nginx starts once the `lab` phase has put the certificate in place.
    await web.wait_for_unit("nginx")
    for url, said in PAGES.items():
        ok, out = await curl(vms, url, shell)
        if not ok or out != said:
            raise AssertionError(f"{url} from the host's shell: {out}")
        web.say(f"the host's shell gets {url}: {out}")

    # A browser shows the page only with a type it displays.
    for url in PAGES:
        done = await anyio.run_process(
            [vms.settings["curl"], "--silent", "--output", "/dev/null", "--write-out", "%{content_type}", url],
            env=shell,
            check=False,
        )
        if not done.stdout.decode().startswith("text/plain"):
            raise AssertionError(f"{url} is {done.stdout.decode()!r}, not text/plain: a browser downloads it")

    public = shell | dict.fromkeys(("SSL_CERT_FILE", "CURL_CA_BUNDLE"), vms.settings["publicRoots"])
    ok, out = await curl(vms, "https://web.lab.test/", public)
    if ok or "(60)" not in out:
        raise AssertionError(f"the public roots alone accepted the lab's certificate: {out}")
    web.say(f"with the public roots alone: {out}")

    ok, out = await curl(vms, "https://nosuch.lab.test/", shell)
    if ok:
        raise AssertionError(f"nosuch.lab.test answered: {out}")
    web.say(f"a name the lab does not have: {out}")
