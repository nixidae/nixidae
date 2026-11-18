"""What a guest boots, the same for every backend.

`vivarium.boot` in Nix: the backend's module writes it, and each backend
takes the parts it can boot. UML and the guest kinds that boot their own
disk have none; their `vivarium.launch.config` says what they boot.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypedDict

from .spec import closed


@closed
class KernelJson(TypedDict):
    """`vivarium.boot.kernel`."""

    image: str
    vmlinux: str | None
    initrd: str | None
    cmdline: str


@closed
class FirmwareJson(TypedDict):
    """`vivarium.boot.firmware`."""

    code: str
    variables: str
    esp: str


@closed
class BootJson(TypedDict):
    """`vivarium.boot`."""

    system: str | None
    kernel: KernelJson | None
    firmware: FirmwareJson | None


@closed
class DisplayJson(TypedDict):
    """`vivarium.display`, when enabled: the screen's size in pixels."""

    width: int
    height: int


class BootspecJson(TypedDict):
    """The part of an `org.nixos.bootspec.v1` document the runner reads.

    The guest's bootloader writes it, so its shape is Nix's: no `closed`.
    """

    kernel: str
    initrd: str
    kernelParams: list[str]
    toplevel: str


@dataclass(frozen=True)
class Kernel:
    """A kernel the backend loads itself, with no bootloader in between."""

    image: Path
    """The kernel a bootloader names: a bzImage on x86_64."""
    vmlinux: Path | None
    """The same kernel as an ELF, for a backend that loads only that (krun).
    None when nothing built it, or when a bootloader chose another kernel."""
    initrd: Path | None
    cmdline: str
    """The parameters, without `init=`: `Boot.cmdline` adds that."""

    @classmethod
    def from_json(cls, data: KernelJson) -> Kernel:
        return cls(
            image=Path(data["image"]),
            vmlinux=_maybe(data["vmlinux"]),
            initrd=_maybe(data["initrd"]),
            cmdline=data["cmdline"],
        )


@dataclass(frozen=True)
class Firmware:
    """UEFI firmware, its variable store's first state, and the ESP image."""

    code: Path
    variables: Path
    """Copied once per run: the copy outlives each reboot, as NVRAM does."""
    esp: Path

    @classmethod
    def from_json(cls, data: FirmwareJson) -> Firmware:
        return cls(code=Path(data["code"]), variables=Path(data["variables"]), esp=Path(data["esp"]))


@dataclass(frozen=True)
class Boot:
    """What a guest boots.

    A guest on firmware boots its first system straight from `kernel`, and
    later systems through its own bootloader on the ESP: see `rebooted`.
    """

    system: str | None
    """The NixOS system: its `init` is PID 1. None for a guest kind with its
    own init, named in `kernel.cmdline`."""
    kernel: Kernel | None
    """None: the firmware's bootloader picks the kernel, or there is no
    kernel to boot (a container)."""
    firmware: Firmware | None

    @classmethod
    def from_json(cls, data: BootJson) -> Boot:
        kernel, firmware = data["kernel"], data["firmware"]
        return cls(
            system=data["system"],
            kernel=Kernel.from_json(kernel) if kernel is not None else None,
            firmware=Firmware.from_json(firmware) if firmware is not None else None,
        )

    @property
    def init(self) -> str | None:
        return f"{self.system}/init" if self.system is not None else None

    def cmdline(self, kernel: Kernel) -> str:
        """*kernel*'s parameters, with this boot's `init=`."""
        return kernel.cmdline if self.init is None else f"{kernel.cmdline} init={self.init}"

    def rebooted(self, chosen: BootspecJson | None) -> Boot:
        """The boot after the guest reboots, given what its bootloader chose.

        Firmware boots whatever its bootloader picks. A direct boot takes
        the bootloader's system, or boots this one again when the guest
        wrote no choice. The ELF form of the kernel stays only while the
        kernel stays.
        """
        if self.firmware is not None:
            return replace(self, system=None, kernel=None)
        if chosen is None:
            return self
        image = Path(chosen["kernel"])
        vmlinux = self.kernel.vmlinux if self.kernel is not None and self.kernel.image == image else None
        return replace(
            self,
            system=chosen["toplevel"],
            kernel=Kernel(
                image=image,
                vmlinux=vmlinux,
                initrd=Path(chosen["initrd"]),
                cmdline=" ".join(chosen["kernelParams"]),
            ),
        )


@dataclass(frozen=True)
class Display:
    """A guest's screen: its size in pixels."""

    width: int
    height: int

    @classmethod
    def from_json(cls, data: DisplayJson) -> Display:
        return cls(width=data["width"], height=data["height"])


def _maybe(path: str | None) -> Path | None:
    return Path(path) if path is not None else None
