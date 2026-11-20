"""The backends come from the `vivarium.backends` entry point group.

This runs against the installed package, so it reads the entry points
pyproject.toml declares, not a list in the code.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from vivarium_runner import MachineSpec, backends
from vivarium_runner.backends import BackendError
from vivarium_runner.backends.container import Container
from vivarium_runner.backends.krun import Krun
from vivarium_runner.backends.qemu import Qemu
from vivarium_runner.backends.uml import Uml
from vivarium_runner.boot import Boot, Kernel, Root, RootFormat
from vivarium_runner.spec import BackendName
from vivarium_runner.testing import machine_json

if TYPE_CHECKING:
    from collections.abc import Mapping


def test_the_four_are_installed() -> None:
    assert {name: type(one) for name, one in backends.available().items()} == {
        "uml": Uml,
        "qemu": Qemu,
        "krun": Krun,
        "container": Container,
    }


def test_get_names_what_is_installed() -> None:
    assert isinstance(backends.get("uml"), Uml)
    with pytest.raises(ValueError, match="no backend 'vm'; have container, krun, qemu, uml"):
        backends.get("vm")


MINE = Path("/home/me/linux/vmlinux")
UML = {"egress": "/e"}
QEMU = {"qemu": "/q", "qemuImg": "/i", "virtiofsd": "/v", "egress": "/e"}
KRUN = {"krun": "/k", "qemuImg": "/i", "acpi": False, "egress": "/e"}
KERNEL = Kernel(image=Path("/nix/store/bzImage"), vmlinux=Path("/nix/store/vmlinux"), initrd=Path("/i"), cmdline="")
NIXOS = Boot(system="/nix/store/s", kernel=KERNEL, firmware=None)


def _spec(backend_name: BackendName, config: Mapping[str, object], boot: Boot | None = None) -> MachineSpec:
    data = machine_json("one", backend_name)
    data["launch"] = {"backend": backend_name, "config": dict(config)}
    return replace(MachineSpec.from_json(data), boot=boot)


class TestWithKernel:
    def test_uml_names_it_in_boot(self) -> None:
        spec = _spec(BackendName.UML, UML, NIXOS)
        got = Uml.with_kernel(spec, MINE)
        assert got.boot == replace(NIXOS, kernel=replace(KERNEL, image=MINE))
        assert spec.boot == NIXOS, "the spec is not changed"

    def test_qemu_names_it_in_boot(self) -> None:
        got = Qemu().with_kernel(_spec(BackendName.QEMU, QEMU, NIXOS), MINE)
        assert got.boot == replace(NIXOS, kernel=replace(KERNEL, image=MINE))

    def test_qemu_names_a_disk_kind_s_in_boot_too(self) -> None:
        appliance = Boot(system=None, kernel=KERNEL, firmware=None, root=Root(RootFormat.QCOW2, None))
        got = Qemu().with_kernel(_spec(BackendName.QEMU, QEMU, appliance), MINE)
        assert got.boot == replace(appliance, kernel=replace(KERNEL, image=MINE))

    def test_qemu_refuses_a_disk_with_its_own_bootloader(self) -> None:
        own = Boot(system=None, kernel=None, firmware=None, root=Root(RootFormat.QCOW2, None))
        with pytest.raises(BackendError, match="bootloader picks its kernel"):
            Qemu().with_kernel(_spec(BackendName.QEMU, QEMU, own), MINE)

    def test_krun_names_the_vmlinux_and_keeps_the_bzimage(self) -> None:
        got = Krun.with_kernel(_spec(BackendName.KRUN, KRUN, NIXOS), MINE)
        assert got.boot == replace(NIXOS, kernel=replace(KERNEL, vmlinux=MINE))

    def test_a_container_refuses(self) -> None:
        with pytest.raises(BackendError, match="the host's kernel"):
            Container.with_kernel(_spec(BackendName.CONTAINER, {"crun": "/c", "setpriv": "/s", "egress": "/e"}), MINE)
