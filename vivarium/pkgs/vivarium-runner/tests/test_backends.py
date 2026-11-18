"""The backends come from the `vivarium.backends` entry point group.

This runs against the installed package, so it reads the entry points
pyproject.toml declares, not a list in the code.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from vivarium_runner import MachineSpec, backend
from vivarium_runner.backend import BackendError, Container, Krun, Qemu, Uml
from vivarium_runner.spec import BackendName
from vivarium_runner.testing import machine_json

if TYPE_CHECKING:
    from collections.abc import Mapping

    from vivarium_runner.machine import BootSpec


def test_the_four_are_installed() -> None:
    assert {name: type(one) for name, one in backend.available().items()} == {
        "uml": Uml,
        "qemu": Qemu,
        "krun": Krun,
        "container": Container,
    }


def test_get_names_what_is_installed() -> None:
    assert isinstance(backend.get("uml"), Uml)
    with pytest.raises(ValueError, match="no backend 'vm'; have container, krun, qemu, uml"):
        backend.get("vm")


MINE = Path("/home/me/linux/vmlinux")
UML = {"kernel": "/nix/store/k-linux", "bridge": "/b", "passt": "/p"}
QEMU = {"qemu": "/q", "qemuImg": "/i", "virtiofsd": "/v", "passt": "/p"}
KRUN = {"krun": "/k", "qemuImg": "/i"}


def _spec(backend_name: BackendName, config: Mapping[str, object], boot: BootSpec | None = None) -> MachineSpec:
    data = machine_json("one", backend_name)
    data["launch"] = {"backend": backend_name, "config": dict(config)}
    if boot is not None:
        data["boot"] = boot
    return MachineSpec.from_json(data)


class TestWithKernel:
    def test_uml_names_it_in_launch(self) -> None:
        spec = _spec(BackendName.UML, UML)
        got = Uml.with_kernel(spec, MINE)
        assert got.launch is not None
        assert got.launch.config == {**UML, "kernel": str(MINE)}
        assert spec.launch is not None
        assert spec.launch.config["kernel"] == "/nix/store/k-linux", "the spec is not changed"

    def test_qemu_names_it_in_boot(self) -> None:
        got = Qemu().with_kernel(_spec(BackendName.QEMU, QEMU, {"kernel": "/nix/store/b", "initrd": "/i"}), MINE)
        assert got.boot == {"kernel": str(MINE), "initrd": "/i"}

    def test_qemu_names_a_disk_kind_s_in_its_disk(self) -> None:
        disk = {"kernel": "/nix/store/b", "initrd": "/i", "cmdline": "console=ttyS0"}
        got = Qemu().with_kernel(_spec(BackendName.QEMU, {**QEMU, "disk": disk}), MINE)
        assert got.launch is not None
        assert got.launch.config["disk"] == {**disk, "kernel": str(MINE)}

    def test_qemu_refuses_a_firmware_boot(self) -> None:
        with pytest.raises(BackendError, match="firmware picks its kernel"):
            Qemu().with_kernel(_spec(BackendName.QEMU, {**QEMU, "disk": {"format": "qcow2"}}), MINE)

    def test_krun_names_the_vmlinux_and_keeps_the_bzimage(self) -> None:
        boot: BootSpec = {"kernel": "/nix/store/bzImage", "vmlinux": "/nix/store/vmlinux", "initrd": "/i"}
        got = Krun.with_kernel(_spec(BackendName.KRUN, KRUN, boot), MINE)
        assert got.boot == {**boot, "vmlinux": str(MINE)}

    def test_krun_names_a_kind_s_vmlinux_in_launch(self) -> None:
        own = {"vmlinux": "/nix/store/vmlinux", "cmdline": "init=/sbin/dinit"}
        got = Krun.with_kernel(_spec(BackendName.KRUN, {**KRUN, "boot": own}), MINE)
        assert got.launch is not None
        assert got.launch.config["boot"] == {**own, "vmlinux": str(MINE)}

    def test_a_container_refuses(self) -> None:
        with pytest.raises(BackendError, match="the host's kernel"):
            Container.with_kernel(_spec(BackendName.CONTAINER, {"crun": "/c", "setpriv": "/s", "passt": "/p"}), MINE)
