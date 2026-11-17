#!/usr/bin/env python3
"""Wait for every guest to finish booting, and say what did not.

The first phase of nearly every run, and the one whose failure is worth
reading. `degraded` alone tells a reader that something is wrong and not
what -- so the agent's answer carries the failed units or services, and
they go in the error. systemd and dinit guests answer alike.
"""

from __future__ import annotations

from vivarium_runner import Machines
from vivarium_runner.control import AGENT, NONE

BOOT_TIMEOUT = 180


async def test(vms: Machines) -> None:
    for name, vm in vms.items():
        if vm.control_kind == NONE:
            # Its console said it was up; there is no systemd to ask.
            print(f"[boot] {name} is up, with no control channel")
            continue
        if vm.control_kind != AGENT:
            # No systemd to ask. Its start logged in and applied its config.
            print(f"[boot] {name} is up on its {vm.control_kind} console")
            continue
        report = await vm.system_state(timeout=BOOT_TIMEOUT)
        if report["state"] == "running":
            print(f"[boot] {name} is running")
            continue
        failed = "\n".join(f"  {unit}" for unit in report["failed"]) or "  (none)"
        if report["state"] == "starting":
            raise AssertionError(f"{name} did not finish booting within {BOOT_TIMEOUT}s, with these failed:\n{failed}")
        raise AssertionError(f"{name} came up {report['state']!r}, with these failed:\n{failed}")
