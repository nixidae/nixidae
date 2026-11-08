from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from vivarium.session import with_kernel

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
