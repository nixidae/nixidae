"""A backend refuses a `vivarium.launch` it cannot read, and names the guest.

`checks.lan` and the other guest checks prove a good config boots; this
holds the refusals, which no guest check reaches.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest
from vivarium_runner.agent import virtio_port
from vivarium_runner.backend import BackendError, Krun, KrunConfig, Qemu, QemuConfig, Uml, UmlConfig
from vivarium_runner.machine import LaunchSpec, MachineSpec

GOOD = {
    "kernel": "/nix/store/k/linux",
    "bridge": "/nix/store/b/bin/uml-passt-bridge",
    "passt": "/nix/store/p/bin/passt",
}
QEMU = {
    "qemu": "/nix/store/q/bin/qemu-system-x86_64",
    "qemuImg": "/nix/store/q/bin/qemu-img",
    "virtiofsd": "/nix/store/v/bin/virtiofsd",
    "passt": "/nix/store/p/bin/passt",
}


def uml(config: Mapping[str, object] | None, backend: str = "uml") -> MachineSpec:
    launch = None if config is None else LaunchSpec(backend, config)
    return MachineSpec(name="one", backend="uml", launch=launch)


def qemu(config: Mapping[str, object]) -> MachineSpec:
    return MachineSpec(name="q", backend="qemu", launch=LaunchSpec("qemu", config))


def test_a_good_config_reads() -> None:
    config = UmlConfig.of(uml(GOOD))
    assert config.kernel == Path(GOOD["kernel"])
    assert config.bridge == Path(GOOD["bridge"])
    assert config.qemu_img is None, "an optional field that is absent reads as None"


@pytest.mark.parametrize(
    ("spec", "says"),
    [
        (uml(None), "one: vivarium.launch is not set"),
        (uml(GOOD, backend="qemu"), "vivarium.launch.backend is 'qemu'"),
        (uml({**GOOD, "kernal": "/x"}), "does not know: kernal"),
        (uml({"kernel": GOOD["kernel"], "passt": GOOD["passt"]}), "needs: bridge"),
    ],
)
def test_uml_refuses(spec: MachineSpec, says: str) -> None:
    with pytest.raises(BackendError, match=says):
        Uml.validate(spec)


def test_ocr_follows_the_display_tools() -> None:
    assert Qemu().ocr(qemu(QEMU)) is None
    tools = {"tesseract": "/nix/store/t/bin/tesseract", "magick": "/nix/store/m/bin/magick"}
    assert QemuConfig.of(qemu({**QEMU, **tools})).ocr == (Path(tools["tesseract"]), Path(tools["magick"]))


def test_ocr_needs_both_tools() -> None:
    with pytest.raises(BackendError, match="OCR needs both"):
        Qemu().validate(qemu({**QEMU, "tesseract": "/nix/store/t/bin/tesseract"}))


def test_from_json_carries_launch() -> None:
    spec = MachineSpec.from_json({"name": "one", "launch": {"backend": "uml", "config": GOOD}})
    assert spec.launch == LaunchSpec("uml", GOOD)
    assert MachineSpec.from_json({"name": "two"}).launch is None


SEED = {"seed": "/nix/store/s/seed.img", "partition": 3, "init": "/usr/lib/systemd/systemd", "kernelParams": []}


def test_a_guest_kind_root_reads() -> None:
    vyos = UmlConfig.of(uml({**GOOD, "root": {"initrd": "/nix/store/i/initrd", "kernelParams": ["boot=live"]}}))
    assert vyos.root is not None and vyos.root.initrd == Path("/nix/store/i/initrd")
    image = UmlConfig.of(uml({**GOOD, "qemuImg": "/nix/store/q/bin/qemu-img", "root": SEED}))
    assert image.root is not None and image.root.partition == 3


@pytest.mark.parametrize(
    ("root", "says"),
    [
        (SEED, "a cloud image needs qemuImg"),
        ({"initrd": "/nix/store/i/initrd"}, "root lacks fields the backend needs: kernelParams"),
        ({**SEED, "partiton": 3}, "root has fields the backend does not know: partiton"),
    ],
)
def test_uml_refuses_a_bad_root(root: dict[str, object], says: str) -> None:
    with pytest.raises(BackendError, match=says):
        Uml.validate(uml({**GOOD, "root": root}))


@pytest.mark.parametrize(
    ("disk", "says"),
    [
        ({"fromat": "qcow2"}, "q: vivarium.launch.config.disk has fields the backend does not know: fromat"),
        ({"kernel": "/nix/store/k/vmlinuz"}, "names one of kernel and initrd"),
    ],
)
def test_qemu_refuses_a_bad_disk(disk: dict[str, object], says: str) -> None:
    with pytest.raises(BackendError, match=says):
        Qemu().validate(qemu({**QEMU, "disk": disk}))


def test_a_disk_reads() -> None:
    disk = QemuConfig.of(qemu({**QEMU, "disk": {"format": "qcow2", "uplink": False}})).disk
    assert disk is not None and disk.format == "qcow2" and disk.uplink is False and disk.kernel is None


KRUN = {"krun": "/nix/store/k/bin/vivarium-krun", "qemuImg": "/nix/store/q/bin/qemu-img"}
DIRECT = {"vmlinux": "/nix/store/k/vmlinux", "cmdline": "root=/dev/vda rw init=/dinix-boot"}


def krun(config: Mapping[str, object]) -> MachineSpec:
    return MachineSpec(name="k", backend="krun", launch=LaunchSpec("krun", config))


def test_a_krun_boot_reads() -> None:
    assert KrunConfig.of(krun(KRUN)).boot is None, "no boot section is the NixOS system"
    boot = KrunConfig.of(krun({**KRUN, "boot": {**DIRECT, "acpi": True}})).boot
    assert boot is not None and boot.vmlinux == Path(DIRECT["vmlinux"]) and boot.acpi
    assert boot.initrd is None, "a direct boot needs no initrd"


@pytest.mark.parametrize(
    ("boot", "says"),
    [
        ({"vmlinux": DIRECT["vmlinux"]}, "k: vivarium.launch.config.boot lacks fields the backend needs: cmdline"),
        ({**DIRECT, "initramfs": "/x"}, "boot has fields the backend does not know: initramfs"),
    ],
)
def test_krun_refuses_a_bad_boot(boot: dict[str, object], says: str) -> None:
    with pytest.raises(BackendError, match=says):
        Krun.validate(krun({**KRUN, "boot": boot}))


def test_a_virtio_port_is_found_by_name(tmp_path: Path) -> None:
    for port, name in (("vport1p1", "other"), ("vport2p1", "vivarium-agent")):
        (tmp_path / port).mkdir()
        (tmp_path / port / "name").write_text(f"{name}\n")
    assert virtio_port("vivarium-agent", tmp_path) == Path("/dev/vport2p1")
    with pytest.raises(FileNotFoundError, match="no virtio-serial port is named 'absent'"):
        virtio_port("absent", tmp_path)
