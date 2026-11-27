"""checks.browser `fetch`: Chromium as `vivarium browser` prepares it loads lab pages by name.

Headless, with the run's split proxy and the lab root in its NSS database
(vivarium/browser.py). https://web.test/ is port 443. https://alt.test/
has only an SRV record, for web.test:8443, which the router's proxy
follows. Negative control: the same Chromium with no lab root gets no
page.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import anyio
from vivarium import browser, exports, trust
from vivarium_runner import Machines

PLAIN = "https://web.test/"
MOVED = "https://alt.test/"


def page_on(port: int) -> str:
    return f"vivarium lab page on {port}"


async def load(chromium: str, certutil: str, proxy: tuple[str, int], root: Path | None, url: str) -> tuple[str, str]:
    """The page Chromium dumps, and the last lines it wrote to stderr."""
    # --no-sandbox: Chromium's own sandbox needs privileges a Nix build lacks.
    headless = browser.Browser(
        browser.Family.CHROMIUM, (chromium, "--headless=new", "--no-sandbox", "--disable-gpu"), "chromium"
    )
    with tempfile.TemporaryDirectory(prefix="vivarium-browser-") as home:
        argv, extra = browser.prepare(headless, Path(home), proxy, root, "linux", certutil)
        with anyio.fail_after(120):
            done = await anyio.run_process([*argv, "--dump-dom", url], env=os.environ | extra, check=False)
    said = done.stderr.decode(errors="replace").splitlines()[-15:]
    return done.stdout.decode(errors="replace"), "\n".join(said)


async def test(vms: Machines) -> None:
    if vms.exports is None:
        raise AssertionError("the run exports nothing")
    proxy = browser.socks_endpoint(vms.exports.values[exports.PROXY])
    chromium, certutil = vms.settings["chromium"], vms.settings["certutil"]
    root = vms.access / trust.CERTIFICATE
    web = vms["web"]
    for url, port in ((PLAIN, 443), (MOVED, 8443)):
        page, said = await load(chromium, certutil, proxy, root, url)
        if page_on(port) not in page:
            raise AssertionError(f"Chromium with the lab root did not load {url} from port {port}:\n{said}\n{page[-300:]}")
        web.say(f"Chromium loads {url} from port {port}, trusting the lab root")
    page, _ = await load(chromium, certutil, proxy, None, PLAIN)
    if page_on(443) in page:
        raise AssertionError(f"Chromium without the lab root loaded {PLAIN}")
    web.say("without the lab root, Chromium refuses it")
