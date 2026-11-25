"""`vms.skip`: a phase that finds its work done is answered, not skipped."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from vivarium.phases import PhaseState, passed
from vivarium.session import Session
from vivarium.spec import PhaseSpec, Spec
from vivarium_runner import Machines

if TYPE_CHECKING:
    from pathlib import Path

DONE = "async def test(vms):\n    vms.skip('the cluster is kept')\n"
WORK = "async def test(vms):\n    vms.shared['ran'] = True\n"


def test_an_unneeded_phase_lets_the_run_pass() -> None:
    assert passed({"init": PhaseState.UNNEEDED, "check": PhaseState.PASSED})
    assert not passed({"init": PhaseState.SKIPPED, "check": PhaseState.PASSED})


@pytest.mark.anyio
async def test_skip_ends_the_phase_and_the_next_runs(tmp_path: Path) -> None:
    (tmp_path / "init.py").write_text(DONE)
    (tmp_path / "check.py").write_text(WORK)
    init = PhaseSpec(name="init", script=tmp_path / "init.py")
    check = PhaseSpec(name="check", script=tmp_path / "check.py", after=["init"])
    session = Session(Spec(machines=[], phases=[init, check]), tmp_path / "out")
    session.vms = Machines()
    for name in Machines.__annotations__:
        setattr(session.vms, name, None)
    session.vms.shared = {}
    state = await session.run(init)
    assert session.errors == {}
    assert state is PhaseState.UNNEEDED
    assert await session.run(check) is PhaseState.PASSED
    assert session.vms.shared == {"ran": True}
    session.write_output()
    document = json.loads((tmp_path / "out" / "phases.json").read_text())
    assert document["passed"]
    assert document["phases"][0] == {
        "name": "init",
        "state": "unneeded",
        "error": None,
        "reason": "the cluster is kept",
    }
