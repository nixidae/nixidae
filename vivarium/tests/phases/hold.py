"""A phase that holds its guest until the run is signalled; see `cleanup`."""

from __future__ import annotations

import anyio
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    await vms.one.succeed("true")
    await anyio.sleep(600)
