#!/usr/bin/env python3
"""systemd-boot counts the tries of a new entry and falls back from a bad one.

The first boot is a direct kernel boot, and the guest installs systemd-boot
for it. `good` is booted through the firmware and blessed. `bad` never
reaches `boot-complete.target`: two tries, then systemd-boot picks `good`.
"""

import re

import anyio

from vivarium_runner import MachineError, Machine, Machines

BACK_WITHIN = 300
COUNTED = "/sys/firmware/efi/efivars/LoaderBootCountPath-4a67b082-0a4c-41cf-b6c7-440b29bb8c4f"
COUNTERS = re.compile(r"\+(\d+)(?:-(\d+))?\.conf$")


async def boot_id(vm: Machine) -> str:
    return (await vm.succeed("cat /proc/sys/kernel/random/boot_id")).strip()


async def reboot(vm: Machine) -> None:
    first = await boot_id(vm)
    # Detached, so the command returns before the agent goes down.
    await vm.succeed("systemd-run --on-active=1 systemctl reboot")
    with anyio.fail_after(BACK_WITHIN):
        while True:
            await anyio.sleep(2)
            try:
                if await boot_id(vm) != first:
                    return
            except (MachineError, OSError):
                continue


async def booted(vm: Machine) -> str:
    return (await vm.succeed("readlink /run/booted-system")).strip()


async def counters(vm: Machine, toplevel: str) -> tuple[int, int] | None:
    """The entry's tries left and tries done, or None once it is blessed."""
    found = await vm.succeed(f"grep -l 'init={toplevel}/init' /boot/loader/entries/*.conf")
    names = found.split()
    assert len(names) == 1, f"{toplevel} has the entries {names}"
    match = COUNTERS.search(names[0])
    if match is None:
        return None
    return int(match[1]), int(match[2] or 0)


async def boot_into(vm: Machine, target: str) -> None:
    await vm.succeed(f"nix-env --profile /nix/var/nix/profiles/system --set {target}")
    await vm.succeed(f"{target}/bin/switch-to-configuration boot")
    assert await counters(vm, target) == (2, 0), "a new entry does not start at 2 tries"
    await reboot(vm)


async def test(vms: Machines) -> None:
    vm = vms.one
    good = vms.settings["good"]
    bad = vms.settings["bad"]
    await vm.succeed("test -d /sys/firmware/efi")
    await vm.succeed("test -e /boot/loader/loader.conf")

    await boot_into(vm, good)
    assert await booted(vm) == good, f"booted {await booted(vm)}, not good"
    await vm.succeed(f"test -e {COUNTED}")
    await vm.wait_for_unit("systemd-bless-boot.service")
    assert await counters(vm, good) is None, "systemd-bless-boot left good's counter"
    print("[test] good booted through systemd-boot and was blessed", flush=True)

    await boot_into(vm, bad)
    assert await booted(vm) == bad, "the first try did not boot bad"
    assert await counters(vm, bad) == (1, 1), await counters(vm, bad)
    await reboot(vm)
    assert await booted(vm) == bad, "the second try did not boot bad"
    assert await counters(vm, bad) == (0, 2), await counters(vm, bad)
    await reboot(vm)
    now = await booted(vm)
    assert now == good, f"with no tries left systemd-boot booted {now}, not good"
    assert await counters(vm, good) is None
    print("[test] two failed tries of bad, then systemd-boot fell back to good", flush=True)
