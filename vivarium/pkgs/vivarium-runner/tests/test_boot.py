"""What a guest boots after it reboots: `Boot.rebooted`, without a guest."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from vivarium_runner.boot import Boot, BootspecJson, Firmware, Kernel

KERNEL = Kernel(
    image=Path("/nix/store/k/bzImage"),
    vmlinux=Path("/nix/store/v/vmlinux"),
    initrd=Path("/nix/store/i/initrd"),
    cmdline="console=ttyS0",
)
FIRST = Boot(system="/nix/store/first", kernel=KERNEL, firmware=None)


def chosen(kernel: str = "/nix/store/k/bzImage") -> BootspecJson:
    return {
        "kernel": kernel,
        "initrd": "/nix/store/j/initrd",
        "kernelParams": ["console=ttyS0", "quiet"],
        "toplevel": "/nix/store/second",
    }


def test_the_init_follows_the_system() -> None:
    assert FIRST.cmdline(KERNEL) == "console=ttyS0 init=/nix/store/first/init"
    own = Boot(system=None, kernel=replace(KERNEL, cmdline="init=/dinix-boot"), firmware=None)
    assert own.kernel is not None and own.cmdline(own.kernel) == "init=/dinix-boot"


def test_no_choice_boots_the_same_again() -> None:
    assert FIRST.rebooted(None) is FIRST


def test_the_bootloader_s_choice_is_booted() -> None:
    after = FIRST.rebooted(chosen())
    assert after.system == "/nix/store/second"
    assert after.kernel == Kernel(
        image=KERNEL.image, vmlinux=KERNEL.vmlinux, initrd=Path("/nix/store/j/initrd"), cmdline="console=ttyS0 quiet"
    )


def test_another_kernel_drops_the_elf() -> None:
    """Nothing built the new kernel's ELF, so krun refuses the boot."""
    after = FIRST.rebooted(chosen(kernel="/nix/store/other/bzImage"))
    assert after.kernel is not None and after.kernel.vmlinux is None


def test_firmware_picks_after_the_first_boot() -> None:
    firmware = Firmware(code=Path("/c"), variables=Path("/v"), esp=Path("/e"), smm=False)
    after = replace(FIRST, firmware=firmware).rebooted(chosen())
    assert after == Boot(system=None, kernel=None, firmware=firmware)
