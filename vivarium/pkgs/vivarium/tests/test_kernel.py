from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from vivarium.session import PerGuest, Session, SessionError, with_cpus, with_kernel
from vivarium.spec import PhaseSpec, Spec
from vivarium_runner.boot import Boot, Kernel
from vivarium_runner.machine_spec import Cpus, LaunchSpec
from vivarium_runner.spec import BackendName
from vivarium_runner.testing import machine_json, machine_spec

MINE = Path("/home/me/linux/linux")


class TestWithKernel:
    def test_each_guest_s_backend_names_it(self) -> None:
        uml_config = {"egress": "/e"}
        linux = Kernel(image=Path("/nix/store/k/linux"), vmlinux=None, initrd=None, cmdline="")
        kernel = Kernel(image=Path("/nix/store/bzImage"), vmlinux=Path("/nix/store/vmlinux"), initrd=None, cmdline="")
        boot = Boot(system="/nix/store/s", kernel=kernel, firmware=None)
        krun_config = {"krun": "/k", "qemuImg": "/i", "acpi": False, "egress": "/e"}
        machines = [
            machine_spec("u", boot=replace(boot, kernel=linux), launch=LaunchSpec("uml", uml_config)),
            machine_spec("k", BackendName.KRUN, boot=boot, launch=LaunchSpec("krun", krun_config)),
        ]
        uml, krun = with_kernel(machines, PerGuest(every=MINE))
        assert uml.boot is not None and uml.boot.kernel is not None
        assert uml.boot.kernel.image == MINE
        assert krun.boot is not None and krun.boot.kernel is not None
        assert krun.boot.kernel.vmlinux == MINE
        assert krun.boot.kernel.image == Path("/nix/store/bzImage")

    def test_a_guest_with_no_kernel_of_its_own_refuses_the_run(self) -> None:
        container = machine_spec(
            "c", BackendName.CONTAINER, launch=LaunchSpec("container", {"crun": "/c", "setpriv": "/s", "egress": "/e"})
        )
        with pytest.raises(SessionError, match="--kernel: c: a container guest runs on the host's kernel"):
            with_kernel([container], PerGuest(every=MINE))

    def test_a_named_guest_takes_its_own_and_the_rest_keep_nix_s(self) -> None:
        container = machine_spec(
            "c", BackendName.CONTAINER, launch=LaunchSpec("container", {"crun": "/c", "setpriv": "/s", "egress": "/e"})
        )
        linux = Kernel(image=Path("/nix/store/k/linux"), vmlinux=None, initrd=None, cmdline="")
        uml = machine_spec("u", boot=Boot(system="/s", kernel=linux, firmware=None))
        got_uml, got_container = with_kernel([uml, container], PerGuest(guests={"u": MINE}))
        assert got_uml.boot is not None and got_uml.boot.kernel is not None
        assert got_uml.boot.kernel.image == MINE
        assert got_container == container

    def test_an_unknown_guest_is_refused(self) -> None:
        with pytest.raises(SessionError, match="no guest x to give a kernel"):
            with_kernel([machine_spec("u")], PerGuest(guests={"x": MINE}))


class TestWithCpus:
    def test_every_guest_takes_the_run_s_cpus(self) -> None:
        machines = [machine_json("a"), machine_json("b")]
        machines[0]["cpus"] = 2
        got = with_cpus(machines, PerGuest[Cpus](every={"scale": 0.5}))
        assert [m["cpus"] for m in got] == [{"scale": 0.5}, {"scale": 0.5}]
        assert machines[0]["cpus"] == 2, "the spec is not changed"

    def test_a_named_guest_takes_its_own(self) -> None:
        machines = [machine_json("a"), machine_json("b"), machine_json("c")]
        machines[0]["cpus"] = 2
        machines[2]["cpus"] = 3
        got = with_cpus(machines, PerGuest[Cpus](every=1, guests={"b": 8}))
        assert [m["cpus"] for m in got] == [1, 8, 1]
        alone = with_cpus(machines, PerGuest[Cpus](guests={"b": {"scale": 0.5}}))
        assert [m["cpus"] for m in alone] == [2, {"scale": 0.5}, 3], "a guest not named keeps vivarium.cpus"

    def test_an_unknown_guest_is_refused(self) -> None:
        with pytest.raises(SessionError, match="no guest x"):
            with_cpus([machine_json("a")], PerGuest[Cpus](guests={"x": 2}))


class TestSessionSpecs:
    def test_a_guest_and_a_template_take_their_own_cpus(self, tmp_path: Path) -> None:
        spec = Spec(
            machines=[machine_json("a")], templates=[machine_json("t")], phases=[PhaseSpec(name="x", script=Path("x"))]
        )
        session = Session(spec, tmp_path / "out", cpus=PerGuest[Cpus](guests={"a": 2, "t": 3}))
        guests, templates = session.specs()
        assert [(one.name, one.cpus) for one in guests] == [("a", 2)]
        assert [(one.name, one.cpus) for one in templates] == [("t", 3)]
