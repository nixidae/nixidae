"""What the README does in `vivarium shell`, from the host.

https://hello.default.svc.cluster.local/ reaches the Service's `https`
port, 8443, through its SRV record, and verifies against the lab root in
`SSL_CERT_FILE`. Negative controls: the public roots alone refuse the
certificate, and a Service that does not exist gets no answer.
"""

from __future__ import annotations

import os

import anyio
from vivarium import exports
from vivarium_runner import Machines

URL = "https://hello.default.svc.cluster.local/"
SAID = "hello from the cluster"


async def curl(vms: Machines, url: str, env: dict[str, str]) -> tuple[bool, str]:
    done = await anyio.run_process(
        [vms.settings["curl"], "--silent", "--show-error", "--max-time", "20", url], env=env, check=False
    )
    return done.returncode == 0, (done.stdout or done.stderr).decode().strip()


async def test(vms: Machines) -> None:
    if vms.exports is None:
        raise AssertionError("the run exports nothing")
    shell = os.environ | exports.shell(vms.exports.values, environment=None, out=vms.access.parent)
    cp = vms["cp"]

    ok, out = await curl(vms, URL, shell)
    if not ok or out != SAID:
        raise AssertionError(f"{URL} from the host's shell: {out}")
    cp.say(f"the host's shell gets {URL}: {out}")

    public = shell | dict.fromkeys(("SSL_CERT_FILE", "CURL_CA_BUNDLE"), vms.settings["publicRoots"])
    ok, out = await curl(vms, URL, public)
    if ok or "(60)" not in out:
        raise AssertionError(f"the public roots alone accepted the lab's certificate: {out}")
    cp.say(f"with the public roots alone: {out}")

    ok, out = await curl(vms, "https://nosuch.default.svc.cluster.local/", shell)
    if ok:
        raise AssertionError(f"a Service that does not exist answered: {out}")
    cp.say(f"a Service that does not exist: {out}")
