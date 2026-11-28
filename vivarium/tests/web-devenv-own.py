"""checks.web-devenv-own: the README's own-project dev.nix serves its page by name."""

from __future__ import annotations

import os

import anyio
from vivarium import exports
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    if vms.exports is None:
        raise AssertionError("the run exports nothing")
    shell = os.environ | exports.shell(vms.exports.values, environment=None, out=vms.access.parent)
    web = vms["web"]
    await web.wait_for_unit("nginx")
    done = await anyio.run_process(
        [vms.settings["curl"], "--silent", "--show-error", "--max-time", "20", "https://web.lab.test/"],
        env=shell,
        check=False,
    )
    page = (done.stdout or done.stderr).decode()
    if done.returncode != 0 or "my project, from web.lab.test" not in page:
        raise AssertionError(f"https://web.lab.test/ from the host: {page}")
    web.say("the README's dev.nix serves its page at https://web.lab.test/")
