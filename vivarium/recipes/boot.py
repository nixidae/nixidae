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
        report = await vm.system_state(timeout=180)
        if report["state"] in ("running", "starting"):
            print(f"[boot] {name} is {report['state']}")
            continue
        failed = "\n".join(f"  {unit}" for unit in report["failed"]) or "  (none)"
        raise AssertionError(f"{name} came up {report['state']!r}, with these failed:\n{failed}")
