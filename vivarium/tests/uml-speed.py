#!/usr/bin/env python3
"""The guest runs the patched SMP kernel with two processors.

UML takes its own switches (`ncpus=`, `seccomp_spin=`) off the command
line before Linux sees it, so `nproc` is the proof of `ncpus=`. The
patch's `prefault=` is a Linux parameter: a kernel without the patch
would name it unknown and hand it to init.
"""

from __future__ import annotations

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    one = vms["one"]
    processors = (await one.succeed("nproc")).stdout.strip()
    if processors != "2":
        raise AssertionError(f"nproc says {processors}, not 2")
    version = (await one.succeed("uname -v")).stdout
    if "SMP" not in version:
        raise AssertionError(f"the kernel is not SMP: {version}")
    if "prefault=64K" not in (await one.succeed("cat /proc/cmdline")).stdout:
        raise AssertionError("prefault=64K is not on the kernel command line")
    unknown = (await one.succeed("dmesg | grep 'Unknown kernel command line' || true")).stdout
    if "prefault" in unknown:
        raise AssertionError(f"the kernel does not know prefault=: {unknown}")
