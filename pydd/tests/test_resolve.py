"""The resolver against the example lock file, with no store involved."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from packaging.requirements import Requirement

from pydd.python import Artifact, Target, resolve
from pydd.python._resolve import choose

LOCK = json.loads((Path(__file__).parent.parent / "examples/venv/lock.json").read_text())
TARGET = Target.cpython("3.14", "linux_x86_64", "linux")


@pytest.fixture
def artifacts() -> list[Artifact]:
    return [
        Artifact.from_json({**row, "drv": f"/nix/store/{row['filename']}.drv", "out": "/x"})
        for row in LOCK["artifacts"]
    ]


def test_a_binary_wheel_for_the_target_beats_the_pure_one(artifacts: list[Artifact]) -> None:
    chosen = choose(artifacts, Requirement("charset-normalizer"), TARGET)
    assert chosen is not None
    assert "manylinux2014_x86_64" in chosen.filename


def test_a_marker_excludes_a_package(artifacts: list[Artifact]) -> None:
    assert choose(artifacts, Requirement("colorama"), TARGET) is None


def test_a_name_is_normalised_before_it_is_compared(artifacts: list[Artifact]) -> None:
    chosen = choose(artifacts, Requirement("flit_core>=3.11,<5"), TARGET)
    assert chosen is not None
    assert chosen.package == "flit-core"


def test_an_sdist_gets_its_backend_as_a_node(artifacts: list[Artifact]) -> None:
    plan = resolve(artifacts, LOCK["roots"], LOCK["installer"], [], TARGET)
    idna = plan.nodes["idna-3.18"]
    assert idna.kind == "sdist"
    assert idna.backend_nodes == ("flit-core-4.0.2",)
    assert "flit-core-4.0.2" not in plan.members
    assert sorted(plan.members) == [
        "certifi-2024.8.30",
        "charset-normalizer-3.4.4",
        "idna-3.18",
        "ninja-1.13.0",
    ]
