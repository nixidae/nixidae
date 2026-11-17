"""One round trip to each guest; see `report`."""

from __future__ import annotations

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    for vm in vms.values():
        await vm.succeed("true")
