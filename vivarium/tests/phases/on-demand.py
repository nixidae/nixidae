#!/usr/bin/env python3
"""A phase on `a` sees `a` alone, and `a` is up."""

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    assert sorted(vms) == ["a"], f"the phase sees {sorted(vms)}"
    await vms.a.succeed("true")
    print("[test] a answers")
