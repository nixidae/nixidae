from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from vivarium.spec import Spec, SpecError
from vivarium_runner.spec import BackendName
from vivarium_runner.testing import machine_json


def write(tmp_path: Path, **fields: object) -> Path:
    path = tmp_path / "spec.json"
    path.write_text(json.dumps({"machines": [], **fields}))
    return path


class TestAFieldThisRunnerDoesNotKnow:
    def test_is_refused_by_name(self, tmp_path: Path) -> None:
        """Measured on nixkube: an older runner dropped `pythonPath`
        silently, and a phase failed on the import it existed for."""
        path = write(tmp_path, vivarium="/nix/store/x-vivarium", fromTheFuture=[1])
        with pytest.raises(SpecError, match=r"fromTheFuture.*/nix/store/x-vivarium"):
            Spec.read(path)

    def test_inside_a_phase_too(self, tmp_path: Path) -> None:
        path = write(tmp_path, phases=[{"name": "a", "script": "/x.py", "retries": 3}])
        with pytest.raises(SpecError, match=r"phases\.0\.retries"):
            Spec.read(path)

    def test_in_a_machine(self, tmp_path: Path) -> None:
        machine = {**machine_json("one"), "fromTheFuture": 1}
        with pytest.raises(SpecError, match=r"machines\.0\.fromTheFuture"):
            Spec.read(write(tmp_path, machines=[machine]))

    def test_deep_in_a_machine(self, tmp_path: Path) -> None:
        nic = {"name": "eth1", "nic": 1, "segment": "lan", "addresses": [], "mac": "m", "mtu": 1500, "vlan": 7}
        machine = {**machine_json("one"), "interfaces": [nic]}
        with pytest.raises(SpecError, match=r"machines\.0\.interfaces\.0\.vlan"):
            Spec.read(write(tmp_path, machines=[machine]))

    def test_a_known_spec_reads(self, tmp_path: Path) -> None:
        spec = Spec.read(write(tmp_path, vivarium="/nix/store/x-vivarium", pythonPath=["/lib"]))
        assert spec.vivarium == Path("/nix/store/x-vivarium")
        assert spec.pythonPath == [Path("/lib")]

    def test_in_a_boot(self, tmp_path: Path) -> None:
        kernel = {"image": "/k", "vmlinux": None, "initrd": None, "cmdline": "", "dtb": "/d"}
        machine = {
            **machine_json("one"),
            "boot": {"system": None, "init": None, "kernel": kernel, "firmware": None, "root": None},
        }
        with pytest.raises(SpecError, match=r"machines\.0\.boot\.kernel\.dtb"):
            Spec.read(write(tmp_path, machines=[machine]))


class TestAFieldTheSpecLacks:
    def test_is_refused_by_name(self, tmp_path: Path) -> None:
        machine = {key: value for key, value in machine_json("one").items() if key != "uplinkProxy"}
        with pytest.raises(SpecError, match=r"lacks fields this runner needs: machines\.0\.uplinkProxy.*older"):
            Spec.read(write(tmp_path, machines=[machine]))

    def test_in_a_boot(self, tmp_path: Path) -> None:
        firmware = {"code": "/c", "esp": "/e"}
        machine = {
            **machine_json("one"),
            "boot": {"system": "/s", "init": None, "kernel": None, "firmware": firmware, "root": None},
        }
        with pytest.raises(SpecError, match=r"machines\.0\.boot\.firmware\.variables"):
            Spec.read(write(tmp_path, machines=[machine]))


@pytest.mark.parametrize(
    "kind",
    [{}, {"script": "/x.py", "pytest": {"tests": "/tests"}}],
    ids=["neither", "both"],
)
def test_a_phase_is_a_script_or_a_pytest_run(tmp_path: Path, kind: dict[str, object]) -> None:
    with pytest.raises(ValidationError, match="phase a needs exactly one of script and pytest"):
        Spec.read(write(tmp_path, phases=[{"name": "a", **kind}]))


def test_a_closed_value_is_checked(tmp_path: Path) -> None:
    machine = {**machine_json("one"), "backend": "vm"}
    with pytest.raises(ValidationError, match=r"machines\.0\.backend"):
        Spec.read(write(tmp_path, machines=[machine]))


def test_a_machine_reads_as_the_runner_types_it(tmp_path: Path) -> None:
    [machine] = Spec.read(write(tmp_path, machines=[machine_json("one", BackendName.QEMU)])).machines
    assert machine["backend"] is BackendName.QEMU
