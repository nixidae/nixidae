#!/usr/bin/env python3
"""The initrd unlocks LUKS from the TPM, and asks for the passphrase once it cannot.

The first boot is a direct kernel boot: it formats the blank disk, binds it
to PCR 7 and makes `unlock` the next generation. systemd-boot boots
`unlock` on Secure Boot firmware in Setup Mode, and its initrd unlocks the
disk with no passphrase. With the TPM's key slot gone, the next boot waits
at the passphrase prompt.
"""

from __future__ import annotations

import anyio
from vivarium_runner import Machine, MachineError, Machines

BACK_WITHIN = 300
# The first call after a reboot goes to the agent of the guest that went
# down; see boot-counting.py.
POLL_TIMEOUT = 5
DISK = "/dev/disk/by-id/virtio-secret"
PASSPHRASE = "vivarium"
PCR7 = "/sys/class/tpm/tpm0/pcr-sha256/7"
# The passphrase prompt ends in no newline, and the console is read in
# lines. So the test sees cryptsetup start and never finish.
UNLOCKING = "Starting Cryptography Setup for secret"
UNLOCKED = "Finished Cryptography Setup for secret"
# Long enough for the firmware and the initrd, short enough that the step
# that waits for it to run out does not take minutes.
ASKING_BOOT_TIMEOUT = 60


async def boot_id(vm: Machine, timeout: float | None = None) -> str:
    return (await vm.succeed("cat /proc/sys/kernel/random/boot_id", timeout=timeout)).stdout.strip()


async def reboot(vm: Machine) -> None:
    first = await boot_id(vm)
    # Detached, so the command returns before the agent goes down.
    await vm.succeed("systemd-run --on-active=1 systemctl reboot")
    with anyio.fail_after(BACK_WITHIN):
        while True:
            await anyio.sleep(2)
            try:
                if await boot_id(vm, POLL_TIMEOUT) != first:
                    return
            except (MachineError, OSError):
                continue


async def test(vms: Machines) -> None:
    vm = vms.one
    unlock = vms.settings["unlock"]

    status = (await vm.succeed("bootctl status")).stdout
    print(f"[test] bootctl status:\n{status}", flush=True)
    assert "Secure Boot: disabled (setup)" in status, "the firmware is not in Setup Mode"
    pcr7 = (await vm.succeed(f"cat {PCR7}")).stdout.strip()
    print(f"[test] PCR 7 on the direct boot: {pcr7}", flush=True)
    assert pcr7.strip("0"), "the firmware measured nothing into PCR 7"

    await vm.succeed(f"echo -n {PASSPHRASE} | cryptsetup luksFormat --batch-mode --pbkdf pbkdf2 {DISK} -")
    await vm.succeed(f"PASSWORD={PASSPHRASE} systemd-cryptenroll --tpm2-device=auto --tpm2-pcrs=7 {DISK}")
    await vm.succeed(f"nix-env --profile /nix/var/nix/profiles/system --set {unlock}")
    await vm.succeed(f"{unlock}/bin/switch-to-configuration boot")
    await reboot(vm)

    booted = (await vm.succeed("readlink /run/booted-system")).stdout.strip()
    assert booted == unlock, f"booted {booted}, not unlock"
    pcr7_after = (await vm.succeed(f"cat {PCR7}")).stdout.strip()
    print(f"[test] PCR 7 through systemd-boot: {pcr7_after}", flush=True)
    await vm.succeed("test -b /dev/mapper/secret")
    print("[test] the initrd unlocked the disk from the TPM", flush=True)

    await vm.succeed(f"PASSWORD={PASSPHRASE} systemd-cryptenroll --wipe-slot=tpm2 {DISK}")
    vm.boot_timeout = ASKING_BOOT_TIMEOUT
    await vm.succeed("systemd-run --on-active=1 systemctl reboot")
    await vm.wait_for_console_text(UNLOCKING, timeout=BACK_WITHIN)
    with anyio.fail_after(BACK_WITHIN):
        while True:
            await anyio.sleep(2)
            try:
                await vm.succeed("true", timeout=POLL_TIMEOUT)
            except MachineError as error:
                if "did not come back from its reboot" in str(error):
                    print(f"[test] the run says so: {error}", flush=True)
                    break
    try:
        await vm.wait_for_console_text(UNLOCKED, timeout=1)
    except TimeoutError:
        print("[test] with no TPM key slot the initrd waits for the passphrase", flush=True)
    else:
        raise AssertionError("the initrd unlocked the disk with no TPM key slot")
