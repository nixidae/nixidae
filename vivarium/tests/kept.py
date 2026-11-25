"""One run of an environment that keeps `/var/lib/probe`.

The disk and `vms.state` count the runs apart; they agree only if both
outlived the last run. A run that keeps nothing starts both at zero.
"""

from __future__ import annotations

from vivarium_runner import Machines

PROBE = "/var/lib/probe"


async def test(vms: Machines) -> None:
    one = vms["one"]
    await one.succeed(f"mountpoint -q {PROBE}")
    on_disk = int((await one.succeed(f"cat {PROBE}/runs 2>/dev/null || echo 0")).stdout)
    kept = int(vms.state.get("runs", 0))
    if on_disk != kept:
        raise AssertionError(f"the disk counts {on_disk} runs, vms.state {kept}")
    if kept and vms.previous.get("count") != "passed":
        raise AssertionError(f"run {kept + 1} reads the last run's count as {vms.previous.get('count')!r}")
    await one.succeed(f"echo {kept + 1} > {PROBE}/runs && sync")
    vms.state["runs"] = kept + 1
    one.say(f"run {kept + 1} of this store")
