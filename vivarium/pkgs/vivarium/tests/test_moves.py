"""`Session.run` applies `phases.transition`: the real session, no guest."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from test_kept import session_of
from vivarium.events import Event, Kind
from vivarium.phases import IllegalMove, PhaseState

if TYPE_CHECKING:
    from pathlib import Path


class Kept:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


@pytest.mark.anyio
async def test_a_running_phase_does_not_start_again(tmp_path: Path) -> None:
    (tmp_path / "init.py").write_text("async def test(vms):\n    pass\n")
    session = session_of(tmp_path / "out", tmp_path / "init.py")
    sink = Kept()
    session.tap(sink)
    session.state["init"] = PhaseState.RUNNING
    with pytest.raises(IllegalMove):
        await session.run(session.spec.phases[0])
    assert session.state["init"] is PhaseState.RUNNING
    assert not [e for e in sink.events if e.kind in (Kind.PHASE_STARTED, Kind.PHASE_FINISHED)]


@pytest.mark.anyio
async def test_a_passed_phase_runs_again(tmp_path: Path) -> None:
    (tmp_path / "init.py").write_text("async def test(vms):\n    pass\n")
    session = session_of(tmp_path / "out", tmp_path / "init.py")
    session.state["init"] = PhaseState.PASSED
    assert await session.run(session.spec.phases[0]) is PhaseState.PASSED
