"""A phase that does one small thing; see `interactive`."""

from __future__ import annotations

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    print(f"[test] {(await vms.one.succeed('hostname')).stdout.strip()} says hello")
