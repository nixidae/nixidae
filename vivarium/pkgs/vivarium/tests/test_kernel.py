from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from vivarium.session import CpuOverride, SessionError, with_cpus, with_kernel

if TYPE_CHECKING:
    from vivarium_runner.machine import MachineSpecJson

MINE = Path("/home/me/linux/linux")


class TestWithKernel:
    def test_uml_takes_it_from_launch(self) -> None:
        machines = with_kernel(
            [{"name": "one", "launch": {"backend": "uml", "config": {"kernel": "/nix/store/k-linux", "bridge": "/b"}}}],
            MINE,
        )
        assert machines[0]["launch"] == {"backend": "uml", "config": {"kernel": str(MINE), "bridge": "/b"}}

    def test_qemu_takes_it_from_each_machine(self) -> None:
        machines = with_kernel(
            [
                {"name": "a", "boot": {"kernel": "/nix/store/b", "initrd": "/i"}},
                {"name": "b", "boot": {"kernel": "/x"}},
            ],
            MINE,
        )
        assert [m["boot"]["kernel"] for m in machines] == [str(MINE), str(MINE)]
        assert machines[0]["boot"]["initrd"] == "/i"
        assert "launch" not in machines[0], "a guest without launch gets none"

    def test_a_launch_without_a_kernel_is_left_alone(self) -> None:
        machines = with_kernel([{"name": "c", "launch": {"backend": "container", "config": {}}}], MINE)
        assert machines[0]["launch"] == {"backend": "container", "config": {}}

    def test_the_spec_is_not_changed(self) -> None:
        machines: list[MachineSpecJson] = [
            {"name": "a", "boot": {"kernel": "/nix/store/b"}},
            {"name": "u", "launch": {"backend": "uml", "config": {"kernel": "/nix/store/k"}}},
        ]
        with_kernel(machines, MINE)
        assert machines[0]["boot"]["kernel"] == "/nix/store/b"
        assert machines[1]["launch"]["config"]["kernel"] == "/nix/store/k"


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
