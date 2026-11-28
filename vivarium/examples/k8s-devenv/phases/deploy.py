"""The app, applied from the host as you would in `vivarium shell`: `kubectl apply`."""

from __future__ import annotations

import os

import anyio
from vivarium import exports
from vivarium_runner import Machines


async def kubectl(vms: Machines, *args: str) -> tuple[bool, str]:
    if vms.exports is None:
        raise AssertionError("the run exports nothing")
    shell = os.environ | exports.shell(vms.exports.values, environment=None, out=vms.access.parent)
    done = await anyio.run_process([vms.settings["kubectl"], *args], env=shell, check=False)
    return done.returncode == 0, (done.stdout + done.stderr).decode()


async def must(vms: Machines, *args: str) -> None:
    ok, out = await kubectl(vms, *args)
    if not ok:
        # What the app said is the likely reason a rollout never ends.
        _, logs = await kubectl(vms, "logs", "deployment/hello", "--tail=20")
        raise AssertionError(f"kubectl {' '.join(args)}: {out}\nthe app's last lines:\n{logs}")


async def test(vms: Machines) -> None:
    cp = vms["cp"]
    await must(vms, "apply", "--filename", vms.settings["manifest"])
    await must(vms, "wait", "--for=condition=Ready", "certificate/hello", "--timeout=120s")
    await must(vms, "rollout", "status", "deployment/hello", "--timeout=180s")
    cp.say("the host's kubectl applied the app, and it runs")
