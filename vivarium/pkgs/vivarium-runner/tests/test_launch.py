"""A backend refuses a `vivarium.launch` it cannot read, and names the guest.

`checks.lan` and the other guest checks prove a good config boots; this
holds the refusals, which no guest check reaches.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from vivarium_runner.backends import BackendError
from vivarium_runner.backends.container import Container
from vivarium_runner.backends.krun import Krun, KrunConfig
from vivarium_runner.backends.qemu import BlankDisk, Qemu, QemuConfig, _drive_args
from vivarium_runner.backends.uml import Uml, UmlConfig, processors
from vivarium_runner.boot import Boot, Firmware, Kernel, Root, RootFormat
from vivarium_runner.machine_spec import LaunchSpec, MachineSpec
from vivarium_runner.spec import BackendName
from vivarium_runner.testing import machine_json, machine_spec

GOOD = {"egress": "/nix/store/e/bin/egress"}
LINUX = Kernel(image=Path("/nix/store/k/linux"), vmlinux=None, initrd=None, cmdline="")
UML_NIXOS = Boot(system="/nix/store/s-nixos-system", kernel=LINUX, firmware=None)
QEMU = {
    "qemu": "/nix/store/q/bin/qemu-system-x86_64",
    "qemuImg": "/nix/store/q/bin/qemu-img",
    "virtiofsd": "/nix/store/v/bin/virtiofsd",
    "egress": "/nix/store/e/bin/egress",
}


def uml(config: Mapping[str, object] | None, backend: str = "uml", boot: Boot | None = UML_NIXOS) -> MachineSpec:
    launch = None if config is None else LaunchSpec(backend, config)
    return machine_spec("one", launch=launch, boot=boot)


NIXOS = Boot(
    system="/nix/store/s-nixos-system",
    kernel=Kernel(image=Path("/nix/store/k/bzImage"), vmlinux=None, initrd=Path("/nix/store/i/initrd"), cmdline=""),
    firmware=None,
)


def qemu(config: Mapping[str, object]) -> MachineSpec:
    return machine_spec("q", BackendName.QEMU, launch=LaunchSpec("qemu", config), boot=NIXOS)


def test_a_good_config_reads() -> None:
    config = UmlConfig.of(uml(GOOD))
    assert config.egress == Path(GOOD["egress"])
    assert config.qemu_img is None, "an optional field that is absent reads as None"


@pytest.mark.parametrize(
    ("spec", "says"),
    [
        (uml(None), "one: vivarium.launch is not set"),
        (uml(GOOD, backend="qemu"), "vivarium.launch.backend is 'qemu'"),
        (uml({**GOOD, "kernal": "/x"}), "does not know: kernal"),
        (uml(GOOD, boot=None), "one: a UML guest needs vivarium.boot.kernel"),
        (uml(GOOD, boot=replace(UML_NIXOS, kernel=None)), "needs vivarium.boot.kernel"),
        (
            uml(GOOD, boot=replace(UML_NIXOS, firmware=Firmware(Path("/c"), Path("/v"), Path("/e"), smm=False))),
            "no firmware",
        ),
        (uml(GOOD, boot=replace(UML_NIXOS, root=Root(RootFormat.QCOW2, 3))), "a qcow2 root image needs qemuImg"),
    ],
)
def test_uml_refuses(spec: MachineSpec, says: str) -> None:
    with pytest.raises(BackendError, match=says):
        Uml.validate(spec)


def test_ocr_follows_the_display_tools() -> None:
    assert QemuConfig.of(qemu(QEMU)).ocr is None
    tools = {"tesseract": "/nix/store/t/bin/tesseract", "magick": "/nix/store/m/bin/magick"}
    assert QemuConfig.of(qemu({**QEMU, **tools})).ocr == (Path(tools["tesseract"]), Path(tools["magick"]))


def test_ocr_needs_both_tools() -> None:
    with pytest.raises(BackendError, match="OCR needs both"):
        Qemu().validate(qemu({**QEMU, "tesseract": "/nix/store/t/bin/tesseract"}))


def test_from_json_carries_launch() -> None:
    data = machine_json("one")
    data["launch"] = {"backend": "uml", "config": GOOD}
    assert MachineSpec.from_json(data).launch == LaunchSpec("uml", GOOD)
    assert MachineSpec.from_json(machine_json("two")).launch is None


def test_a_root_reads() -> None:
    assert Root.from_json({"format": "squashfs", "partition": None}) == Root(RootFormat.SQUASHFS, None)
    with pytest.raises(ValueError, match="'vmdk' is not a valid RootFormat"):
        Root.from_json({"format": "vmdk", "partition": None})


VYOS = Boot(
    system=None,
    kernel=replace(LINUX, initrd=Path("/nix/store/i/initrd"), cmdline="boot=live"),
    firmware=None,
    root=Root(RootFormat.SQUASHFS, None),
)


@pytest.mark.parametrize(
    ("config", "boot", "argv"),
    [
        (GOOD, UML_NIXOS, ["ubd0=/run/cow,/nix/store/image", "root=/dev/ubda", "init=/init"]),
        (GOOD, VYOS, ["ubd0r=/nix/store/image", "initrd=/nix/store/i/initrd", "boot=live"]),
        (
            {**GOOD, "seed": "/nix/store/s/seed.img"},
            replace(
                UML_NIXOS, system=None, kernel=replace(LINUX, cmdline="init=/sbin/init"), root=Root(RootFormat.RAW, 3)
            ),
            ["ubd0=/run/cow,/nix/store/image", "ubd1r=/nix/store/s/seed.img", "root=/dev/ubda3", "init=/sbin/init"],
        ),
    ],
)
def test_uml_boots_each_root(config: Mapping[str, object], boot: Boot, argv: list[str]) -> None:
    spec = replace(uml(config, boot=boot), image=Path("/nix/store/image"))
    assert boot.kernel is not None
    root = anyio.run(Uml._root, UmlConfig.of(spec), Path("/run"), spec, boot.root, boot.kernel, None)
    assert root == argv


def test_a_kept_store_is_the_second_ubd() -> None:
    """`modules/kept.nix` mounts `/dev/ubdb`."""
    spec = replace(uml(GOOD, boot=UML_NIXOS), image=Path("/nix/store/image"))
    assert UML_NIXOS.kernel is not None
    root = anyio.run(Uml._root, UmlConfig.of(spec), Path("/run"), spec, None, UML_NIXOS.kernel, Path("/e/disks/one"))
    assert root[:3] == ["ubd0=/run/cow,/nix/store/image", "ubd1=/e/disks/one", "root=/dev/ubda"]


@pytest.mark.parametrize(
    ("smp", "cpus", "args", "noted"),
    [
        (False, 1, [], False),
        (False, 4, [], True),
        (True, 1, ["ncpus=1"], False),
        (True, 4, ["ncpus=4"], False),
    ],
)
def test_uml_passes_ncpus_to_an_smp_kernel_only(smp: bool, cpus: int, args: list[str], noted: bool) -> None:
    got, notes = processors(replace(LINUX, smp=smp), cpus)
    assert got == args
    assert bool(notes) == noted


@pytest.mark.parametrize(
    ("config", "says"),
    [
        ({"disk": {"format": "qcow2"}}, "q: vivarium.launch.config has fields the qemu backend does not know: disk"),
    ],
)
def test_qemu_refuses_an_unknown_section(config: dict[str, object], says: str) -> None:
    with pytest.raises(BackendError, match=says):
        Qemu().validate(qemu({**QEMU, **config}))


def test_blank_disks_read() -> None:
    config = QemuConfig.of(qemu({**QEMU, "disks": [{"name": "secret", "size": 64}]}))
    assert config.disks == (BlankDisk(name="secret", size=64),)
    assert QemuConfig.of(qemu(QEMU)).disks == ()


@pytest.mark.parametrize(
    ("disks", "says"),
    [
        ({"name": "secret", "size": 64}, "q: vivarium.launch.config.disks is not a list"),
        ([{"name": "secret"}], "q: vivarium.launch.config.disks.0 lacks fields the backend needs: size"),
    ],
)
def test_qemu_refuses_bad_blank_disks(disks: object, says: str) -> None:
    with pytest.raises(BackendError, match=says):
        Qemu().validate(qemu({**QEMU, "disks": disks}))


def test_the_root_is_the_first_drive_and_only_blank_disks_have_a_serial() -> None:
    args = _drive_args((10, 11, 12, 13), [("qcow2", None), ("raw", "secret")])
    devices = [args[i + 1] for i, arg in enumerate(args) if arg == "-device"]
    assert devices == ["virtio-blk-pci,drive=drive0", "virtio-blk-pci,drive=drive1,serial=secret"]
    assert "file=/dev/fdset/1,if=none,id=drive0,format=qcow2" in args
    assert args[8:12] == ["-add-fd", "fd=12,set=2", "-add-fd", "fd=13,set=2"]


def test_a_disk_kind_s_hardware_reads() -> None:
    config = QemuConfig.of(qemu({**QEMU, "configDisk": "/nix/store/c/config.img", "uplink": False}))
    assert config.config_disk == Path("/nix/store/c/config.img") and config.uplink is False
    assert QemuConfig.of(qemu(QEMU)).uplink, "a guest has an uplink unless it says otherwise"


KRUN = {
    "krun": "/nix/store/k/bin/vivarium-krun",
    "qemuImg": "/nix/store/q/bin/qemu-img",
    "acpi": True,
    "egress": "/nix/store/e/bin/egress",
}
DIRECT = Kernel(
    image=Path("/nix/store/k/bzImage"),
    vmlinux=Path("/nix/store/k/vmlinux"),
    initrd=None,
    cmdline="root=/dev/vda rw init=/dinix-boot",
)


DINIX = Boot(system=None, kernel=DIRECT, firmware=None)


def krun(config: Mapping[str, object], boot: Boot | None = DINIX) -> MachineSpec:
    return machine_spec("k", BackendName.KRUN, launch=LaunchSpec("krun", config), boot=boot)


def test_a_krun_config_reads() -> None:
    assert KrunConfig.of(krun(KRUN)).acpi
    Krun.validate(krun(KRUN))


FIRMWARE = Firmware(code=Path("/c"), variables=Path("/v"), esp=Path("/e"), smm=False)


@pytest.mark.parametrize(
    ("config", "boot", "says"),
    [
        (
            {k: v for k, v in KRUN.items() if k != "acpi"},
            None,
            "k: vivarium.launch.config lacks fields the krun backend needs: acpi",
        ),
        (KRUN, None, "k: a krun guest needs vivarium.boot.kernel"),
        (KRUN, Boot(system="/s", kernel=None, firmware=None), "needs vivarium.boot.kernel"),
        (KRUN, Boot(system=None, kernel=DIRECT, firmware=FIRMWARE), "boots no firmware"),
        (
            KRUN,
            Boot(system=None, kernel=DIRECT, firmware=None, root=Root(RootFormat.RAW, None)),
            "no guest kind's root",
        ),
        (
            KRUN,
            Boot(system=None, kernel=replace(DIRECT, vmlinux=None), firmware=None),
            "boots only the kernel it started with",
        ),
    ],
)
def test_krun_refuses_a_bad_boot(config: Mapping[str, object], boot: Boot | None, says: str) -> None:
    with pytest.raises(BackendError, match=says):
        Krun.validate(krun(config, boot))


@pytest.mark.parametrize(
    ("boot", "says"),
    [
        (None, "q: vivarium.boot names no kernel and no root image to boot"),
        (Boot(system="/s", kernel=None, firmware=FIRMWARE), "names no kernel and no root image"),
        (Boot(system=None, kernel=None, firmware=None, root=Root(RootFormat.SQUASHFS, None)), "no squashfs"),
    ],
)
def test_qemu_refuses_a_spec_with_nothing_to_boot(boot: Boot | None, says: str) -> None:
    with pytest.raises(BackendError, match=says):
        Qemu().validate(replace(qemu(QEMU), boot=boot))


def test_qemu_boots_a_root_image_through_its_own_bootloader() -> None:
    Qemu().validate(
        replace(qemu(QEMU), boot=Boot(system=None, kernel=None, firmware=None, root=Root(RootFormat.QCOW2, None)))
    )


CONTAINER = {"crun": "/c", "setpriv": "/s", "egress": "/e"}


@pytest.mark.parametrize(
    ("boot", "says"),
    [
        (None, "c: a container guest needs vivarium.boot.system"),
        (Boot(system="/s", kernel=DIRECT, firmware=None), "boots no kernel, no firmware and no root image"),
        (Boot(system="/s", kernel=None, firmware=None, root=Root(RootFormat.RAW, None)), "no root image"),
    ],
)
def test_a_container_refuses_a_boot_it_cannot_run(boot: Boot | None, says: str) -> None:
    spec = machine_spec("c", BackendName.CONTAINER, launch=LaunchSpec("container", CONTAINER), boot=boot)
    with pytest.raises(BackendError, match=says):
        Container.validate(spec)
