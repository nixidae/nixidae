from __future__ import annotations

from pathlib import Path

from vivarium.session import Session
from vivarium.spec import PhaseSpec, Spec
from vivarium_runner import Machines

PHASE = PhaseSpec(name="check", script=Path("x"))


def test_a_phases_vms_carries_every_attribute_machines_declares(tmp_path: Path) -> None:
    """`view` copies each attribute by hand, and one it forgets is an
    AttributeError in a phase: `vms.access` was, measured on checks.k8s-host."""
    session = Session(Spec(machines=[], phases=[PHASE]), tmp_path / "out")
    whole = Machines()
    declared = set(Machines.__annotations__)
    assert {"access", "artifacts", "settings"} <= declared, "the annotations are not where this test reads them"
    for name in declared:
        setattr(whole, name, f"the run's {name}")
    session.vms = whole
    view = session.view(PHASE)
    missing = {name for name in declared - {"phase", "pytest_args"} if getattr(view, name, None) != f"the run's {name}"}
    assert missing == set()
    assert view.phase == "check"
