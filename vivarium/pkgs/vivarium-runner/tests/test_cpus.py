from __future__ import annotations

import os

import pytest
from vivarium_runner.machine import MachineSpec, host_cpus, resolve_cpus


class TestResolveCpus:
    def test_a_count_is_kept(self) -> None:
        assert resolve_cpus(3, 16) == 3

    @pytest.mark.parametrize(("scale", "host", "cpus"), [(1, 16, 16), (0.5, 16, 8), (0.5, 3, 2), (2, 4, 8), (0.01, 4, 1)])
    def test_a_scale_is_a_share_of_the_host(self, scale: float, host: int, cpus: int) -> None:
        assert resolve_cpus({"scale": scale}, host) == cpus


class TestHostCpus:
    def test_a_build_s_cores(self) -> None:
        assert host_cpus({"NIX_BUILD_CORES": "6"}) == 6

    @pytest.mark.parametrize("granted", ["0", "", "x"])
    def test_otherwise_the_affinity_mask(self, granted: str) -> None:
        assert host_cpus({"NIX_BUILD_CORES": granted}) == len(os.sched_getaffinity(0))


def test_the_spec_holds_the_resolved_count() -> None:
    spec = MachineSpec.from_json({"name": "one", "cpus": {"scale": 1}})
    assert spec.cpus == host_cpus()
