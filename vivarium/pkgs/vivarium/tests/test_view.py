from __future__ import annotations

from dataclasses import fields
from pathlib import Path

import pytest
from vivarium.session import Session
from vivarium.spec import PhaseSpec, Spec
from vivarium_runner import MachineError, Machines, RunContext

PHASE = PhaseSpec(name="check", script=Path("x"))


def test_a_phases_vms_reads_every_run_value_from_the_one_context(tmp_path: Path) -> None:
    """A phase's `vms` holds the run's context, not a copy: one value the
    session left out was an AttributeError in a phase (`vms.access`,
    measured on checks.k8s-host)."""
    session = Session(Spec(machines=[], phases=[PHASE]), tmp_path / "out")
    context = RunContext(artifacts=tmp_path / "artifacts", access=tmp_path / "access", settings={"k": 1})
    session.vms = Machines(context=context)
    view = session.view(PHASE)
    assert view.context is context
    for one in fields(RunContext):
        assert getattr(view, one.name) is getattr(context, one.name), one.name
    assert view.phase == "check"


def test_a_reload_reaches_the_next_phase(tmp_path: Path) -> None:
    session = Session(Spec(machines=[], phases=[PHASE], settings={"v": 1}), tmp_path / "out")
    session.vms = Machines(context=RunContext(artifacts=tmp_path, access=tmp_path, settings={"v": 1}))
    shared = session.vms.shared
    session.reload(Spec(machines=[], phases=[PHASE], settings={"v": 2}))
    view = session.view(PHASE)
    assert view.settings == {"v": 2}
    assert view.shared is shared, "a reload kept the phases' findings apart"


def test_guests_with_no_run_say_so() -> None:
    """Not `no machine 'settings'`: the value is missing, not a guest."""
    with pytest.raises(MachineError, match="belong to no run"):
        _ = Machines().settings


def test_a_view_shares_the_context_and_not_the_guests(tmp_path: Path) -> None:
    whole = Machines(context=RunContext(artifacts=tmp_path, access=tmp_path))
    view = whole.view([], "p")
    assert view.context is whole.context
    assert list(view) == []
