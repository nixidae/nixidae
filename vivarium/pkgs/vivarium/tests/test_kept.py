from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest
from vivarium import kept, places
from vivarium.phases import PhaseState
from vivarium.session import Session
from vivarium.spec import PhaseSpec, Spec
from vivarium_runner import Machines, RunContext

if TYPE_CHECKING:
    from pathlib import Path

INIT = """async def test(vms):
    if vms.previous.get("init") == "passed" and vms.state.get("initialized"):
        vms.skip("kept")
    vms.state["initialized"] = True
"""


def session_of(out: Path, script: Path) -> Session:
    session = Session(Spec(machines=[], phases=[PhaseSpec(name="init", script=script)]), out)
    session.vms = Machines(context=RunContext(artifacts=out, access=out))
    return session


def with_kept(session: Session) -> None:
    """The kept keys and outcomes in the context, as `Session.prepare` puts them."""
    assert session.vms is not None
    assert session.vms.context is not None
    keys, outcomes = session.kept.keys, dict(session.kept.outcomes)
    session.vms.context = replace(session.vms.context, state=keys, previous=outcomes)


async def drive(out: Path, script: Path) -> PhaseState:
    session = session_of(out, script)
    session.kept = await kept.load(out)
    with_kept(session)
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
async def test_a_value_json_cannot_hold_fails_the_phase_and_nothing_after(tmp_path: Path) -> None:
    (tmp_path / "bad.py").write_text("async def test(vms):\n    vms.state['x'] = object()\n")
    out = places.environment("lab")
    out.mkdir(parents=True)
    (out / kept.FILE).write_text(json.dumps({"keys": {"kept": 1}, "outcomes": {}}))
    session = session_of(out, tmp_path / "bad.py")
    session.kept = await kept.load(out)
    with_kept(session)
    assert session.vms is not None
    assert await session.run(session.spec.phases[0]) is PhaseState.FAILED
    assert "vms.state['x']" in session.errors["init"]
    # Back as last saved, in the dict the phases hold.
    assert session.vms.state == {"kept": 1}
    # The verdict writes everything after it: the report, and the outcomes.
    session.write_output()
    assert (out / "report.json").exists()
    assert json.loads((out / kept.FILE).read_text()) == {"keys": {"kept": 1}, "outcomes": {"init": "failed"}}


@pytest.mark.parametrize("text", ["", "[]", '{"keys": {}}', '{"keys": {}, "outcomes": {"a": 1}}'])
def test_a_damaged_file_raises(tmp_path: Path, text: str) -> None:
    with pytest.raises(kept.KeptError):
        kept.parse(tmp_path / kept.FILE, text)
