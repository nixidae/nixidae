from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from vivarium import kept, places
from vivarium.phases import PhaseState
from vivarium.session import Session
from vivarium.spec import PhaseSpec, Spec
from vivarium_runner import Machines

if TYPE_CHECKING:
    from pathlib import Path

INIT = """async def test(vms):
    if vms.previous.get("init") == "passed" and vms.state.get("initialized"):
        vms.skip("kept")
    vms.state["initialized"] = True
"""


def session_of(out: Path, script: Path) -> Session:
    session = Session(Spec(machines=[], phases=[PhaseSpec(name="init", script=script)]), out)
    session.vms = Machines()
    for name in Machines.__annotations__:
        setattr(session.vms, name, None)
    return session


async def drive(out: Path, script: Path) -> PhaseState:
    session = session_of(out, script)
    session.kept = await kept.load(out)
    assert session.vms is not None
    session.vms.state = session.kept.keys
    session.vms.previous = dict(session.kept.outcomes)
    state = await session.run(session.spec.phases[0])
    session.write_output()
    return state


@pytest.mark.anyio
async def test_an_environment_skips_on_its_second_run(tmp_path: Path) -> None:
    (tmp_path / "init.py").write_text(INIT)
    out = places.environment("lab")
    out.mkdir(parents=True)
    assert await drive(out, tmp_path / "init.py") is PhaseState.PASSED
    assert json.loads((out / kept.FILE).read_text()) == {
        "keys": {"initialized": True},
        "outcomes": {"init": "passed"},
    }
    assert await drive(out, tmp_path / "init.py") is PhaseState.UNNEEDED
    assert json.loads((out / kept.FILE).read_text())["outcomes"] == {"init": "unneeded"}


@pytest.mark.anyio
async def test_a_plain_out_keeps_nothing(tmp_path: Path) -> None:
    (tmp_path / "init.py").write_text(INIT)
    out = tmp_path / "out"
    assert await drive(out, tmp_path / "init.py") is PhaseState.PASSED
    assert await drive(out, tmp_path / "init.py") is PhaseState.PASSED
    assert not (out / kept.FILE).exists()


@pytest.mark.anyio
async def test_a_value_json_cannot_hold_fails_the_phase(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("async def test(vms):\n    vms.state['x'] = object()\n")
    out = places.environment("lab")
    out.mkdir(parents=True)
    session = session_of(out, tmp_path / "bad.py")
    session.kept = await kept.load(out)
    assert session.vms is not None
    session.vms.state = session.kept.keys
    assert await session.run(session.spec.phases[0]) is PhaseState.FAILED
    assert "JSON" in session.errors["init"]


@pytest.mark.parametrize("text", ["", "[]", '{"keys": {}}', '{"keys": {}, "outcomes": {"a": 1}}'])
def test_a_damaged_file_raises(tmp_path: Path, text: str) -> None:
    with pytest.raises(kept.KeptError):
        kept.parse(tmp_path / kept.FILE, text)
