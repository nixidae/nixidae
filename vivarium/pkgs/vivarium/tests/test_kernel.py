from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from vivarium.session import CpuOverride, SessionError, with_cpus, with_kernel
from vivarium_runner import MachineSpec

if TYPE_CHECKING:
    from vivarium_runner.machine import MachineSpecJson

MINE = Path("/home/me/linux/linux")


class TestWithKernel:
    def test_each_guest_s_backend_names_it(self) -> None:
        machines = [
            MachineSpec.from_json(
                {
                    "name": "u",
                    "backend": "uml",
                    "launch": {"backend": "uml", "config": {"kernel": "/nix/store/k", "bridge": "/b", "passt": "/p"}},
                }
            ),
            MachineSpec.from_json(
                {
                    "name": "k",
                    "backend": "krun",
                    "boot": {"kernel": "/nix/store/bzImage", "vmlinux": "/nix/store/vmlinux"},
                    "launch": {"backend": "krun", "config": {"krun": "/k", "qemuImg": "/i"}},
                }
            ),
        ]
        uml, krun = with_kernel(machines, MINE)
        assert uml.launch is not None
        assert uml.launch.config["kernel"] == str(MINE)
        assert krun.boot["vmlinux"] == str(MINE)
        assert krun.boot["kernel"] == "/nix/store/bzImage"

    def test_a_guest_with_no_kernel_of_its_own_refuses_the_run(self) -> None:
        container = MachineSpec.from_json(
            {
                "name": "c",
                "backend": "container",
                "launch": {"backend": "container", "config": {"crun": "/c", "setpriv": "/s", "passt": "/p"}},
            }
        )
        with pytest.raises(SessionError, match="--kernel: c: a container guest runs on the host's kernel"):
            with_kernel([container], MINE)


class TestWithCpus:
    def test_every_guest_takes_the_run_s_cpus(self) -> None:
        machines: list[MachineSpecJson] = [{"name": "a", "cpus": 2}, {"name": "b"}]
        got = with_cpus(machines, CpuOverride(every={"scale": 0.5}))
        assert [m["cpus"] for m in got] == [{"scale": 0.5}, {"scale": 0.5}]
        assert machines[0]["cpus"] == 2, "the spec is not changed"

    def test_a_named_guest_takes_its_own(self) -> None:
        machines: list[MachineSpecJson] = [{"name": "a", "cpus": 2}, {"name": "b"}, {"name": "c", "cpus": 3}]
        got = with_cpus(machines, CpuOverride(every=1, guests={"b": 8}))
        assert [m.get("cpus") for m in got] == [1, 8, 1]
        alone = with_cpus(machines, CpuOverride(guests={"b": {"scale": 0.5}}))
        assert [m.get("cpus") for m in alone] == [2, {"scale": 0.5}, 3], "a guest not named keeps vivarium.cpus"

    def test_an_unknown_guest_is_refused(self) -> None:
        with pytest.raises(SessionError, match="no guest x"):
            with_cpus([{"name": "a"}], CpuOverride(guests={"x": 2}))
