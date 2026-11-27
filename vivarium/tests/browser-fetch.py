"""checks.browser `fetch`: Chromium as `vivarium browser` prepares it loads https://web.test/.

Headless, with the run's split proxy and the lab root in its NSS database
(vivarium/browser.py). Negative control: the same Chromium with no lab
root gets no page.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import anyio
from vivarium import browser, exports, trust
from vivarium_runner import Machines

URL = "https://web.test/"
PAGE = "vivarium lab page"


async def load(chromium: str, certutil: str, proxy: tuple[str, int], root: Path | None) -> tuple[str, str]:
    """The page Chromium dumps, and the last lines it wrote to stderr."""
    # --no-sandbox: Chromium's own sandbox needs privileges a Nix build lacks.
    headless = browser.Browser(
        browser.Family.CHROMIUM, (chromium, "--headless=new", "--no-sandbox", "--disable-gpu"), "chromium"
    )
    with tempfile.TemporaryDirectory(prefix="vivarium-browser-") as home:
        argv, extra = browser.prepare(headless, Path(home), proxy, root, "linux", certutil)
        with anyio.fail_after(120):
            done = await anyio.run_process([*argv, "--dump-dom", URL], env=os.environ | extra, check=False)
    said = done.stderr.decode(errors="replace").splitlines()[-15:]
    return done.stdout.decode(errors="replace"), "\n".join(said)


async def test(vms: Machines) -> None:
    if vms.exports is None:
        raise AssertionError("the run exports nothing")
    proxy = browser.socks_endpoint(vms.exports.values[exports.PROXY])
    chromium, certutil = vms.settings["chromium"], vms.settings["certutil"]
    root = vms.access / trust.CERTIFICATE
    page, said = await load(chromium, certutil, proxy, root)
    if PAGE not in page:
        raise AssertionError(f"Chromium with the lab root did not load {URL}:\n{said}")
    vms["web"].say(f"Chromium loads {URL} through the proxy, trusting the lab root")
    page, _ = await load(chromium, certutil, proxy, None)
    if PAGE in page:
        raise AssertionError(f"Chromium without the lab root loaded {URL}")
    vms["web"].say("without the lab root, Chromium refuses it")
