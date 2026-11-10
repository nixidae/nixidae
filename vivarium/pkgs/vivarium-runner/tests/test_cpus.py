from __future__ import annotations

import os
from pathlib import Path

import pytest
from vivarium_runner.machine import (
    MEMORY_SCALE_FLOOR_MIB,
    MachineSpec,
    host_available_mib,
    host_cpus,
    resolve_cpus,
    resolve_memory,
)


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


class TestResolveMemory:
    def test_a_size_is_kept(self) -> None:
        assert resolve_memory("3072M", lambda: 1) == "3072M"

    @pytest.mark.parametrize(("scale", "available", "memory"), [(1, 40000, "40000M"), (0.5, 40000, "20000M")])
    def test_a_scale_is_a_share_of_the_available_memory(self, scale: float, available: int, memory: str) -> None:
        assert resolve_memory({"scale": scale}, lambda: available) == memory

    def test_a_scale_never_goes_under_the_floor(self) -> None:
        assert resolve_memory({"scale": 0.01}, lambda: 1000) == f"{MEMORY_SCALE_FLOOR_MIB}M"

    def test_the_host_available_memory_is_read(self, tmp_path: Path) -> None:
        meminfo = tmp_path / "meminfo"
        meminfo.write_text("MemTotal:       65536000 kB\nMemAvailable:   40960000 kB\n")
        assert host_available_mib(meminfo) == 40000


def test_the_spec_holds_the_resolved_memory() -> None:
    spec = MachineSpec.from_json({"name": "one", "memory": {"scale": 1}})
    assert spec.memory == f"{max(MEMORY_SCALE_FLOOR_MIB, host_available_mib())}M"
