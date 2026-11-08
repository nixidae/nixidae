"""A backend refuses a `vivarium.launch` it cannot read, and names the guest.

`checks.lan` and the other guest checks prove a good config boots; this
holds the refusals, which no guest check reaches.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from vivarium_runner.backend import BackendError, Qemu, QemuConfig, Uml, UmlConfig
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


def uml(config: dict[str, str] | None, backend: str = "uml") -> MachineSpec:
    launch = None if config is None else LaunchSpec(backend, config)
    return MachineSpec(name="one", backend="uml", launch=launch)


def qemu(config: dict[str, str]) -> MachineSpec:
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
