#!/usr/bin/env python3
"""How fast the guest reaches the internet through its uplink: speedtest-cli's answer."""

from __future__ import annotations

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    print((await vms.one.succeed("speedtest-cli --simple", timeout=600)).stdout)
