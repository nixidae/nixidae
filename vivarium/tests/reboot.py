#!/usr/bin/env python3
"""A QEMU guest that reboots comes back on the system its bootloader names.

`next` is the booted system plus a kernel parameter, an initrd file and a
file in /etc. The guest makes it its boot default and reboots itself, as a
host does after `switch-to-configuration boot`. The runner must boot the
new kernel parameters and initrd, on the disk the guest wrote.
"""

import json

import anyio
from vivarium_runner import MachineError, Machines

PARAM = "vivarium.reboot=next"
BACK_WITHIN = 300


async def test(vms: Machines) -> None:
    vm = vms.one
    target = vms.settings["next"]
    boot_id = "cat /proc/sys/kernel/random/boot_id"
    first = (await vm.succeed(boot_id)).strip()

    await vm.succeed("echo kept > /var/lib/vivarium-reboot")
    await vm.succeed(f"nix-env --profile /nix/var/nix/profiles/system --set {target}")
    await vm.succeed(f"{target}/bin/switch-to-configuration boot")
    spec = json.loads((vms.artifacts / "one" / "bootloader.json").read_text())
    chosen = spec["org.nixos.bootspec.v1"]["toplevel"]
    assert chosen == target, f"the bootloader names {chosen}, not {target}"

    # Detached, so the command returns before the agent goes down.
    await vm.succeed("systemd-run --on-active=1 systemctl reboot")
    with anyio.fail_after(BACK_WITHIN):
        while True:
            await anyio.sleep(2)
            try:
                now = (await vm.succeed(boot_id)).strip()
            except (MachineError, OSError):
                continue
            if now != first:
                break
    print("[test] the guest came back with a new boot id", flush=True)

    booted = (await vm.succeed("readlink /run/booted-system")).strip()
    assert booted == target, f"booted {booted}, not {target}"
    cmdline = await vm.succeed("cat /proc/cmdline")
    assert PARAM in cmdline.split(), f"{PARAM} is not on {cmdline!r}"
    marker = (await vm.succeed("cat /etc/vivarium-reboot")).strip()
    assert marker == "next", f"/etc/vivarium-reboot says {marker!r}"
    kept = (await vm.succeed("cat /var/lib/vivarium-reboot")).strip()
    assert kept == "kept", "the disk did not survive the reboot"
    await vms.two.succeed("ping -c 1 -W 5 10.105.0.1")
    print(f"[test] the guest booted {target}, on its disk and its segment", flush=True)
