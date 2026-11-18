"""What the runner says about one guest reaches `events.jsonl` with that guest's name."""

from __future__ import annotations

import io
from pathlib import Path

import anyio
from vivarium.events import Event, Kind, Level
from vivarium.session import Session
from vivarium.spec import Spec
from vivarium_runner import Machine, Machines


class Collect:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def emit(self, event: Event) -> None:
        self.events.append(event)

    def close(self) -> None:
        pass


class Stuck(Machine):
    """A guest whose poweroff fails."""

    def __init__(self) -> None:
        pass

    @property
    def name(self) -> str:
        return "cp"

    async def shutdown(self, *, kill: bool = False) -> None:
        raise RuntimeError("the agent hung up")


def session(tmp_path: Path, sink: Collect) -> Session:
    return Session(Spec(machines=[], phases=[]), tmp_path / "out", sink=sink)


def test_a_guest_that_does_not_shut_down_cleanly_is_an_error_by_name(tmp_path: Path) -> None:
    sink = Collect()
    run = session(tmp_path, sink)
    run.vms = Machines({"cp": Stuck()})
    anyio.run(run.teardown)
    [event] = sink.events
    assert (event.kind, event.level, event.machine) == (Kind.ERROR, Level.ERROR, "cp")
    assert event.text == "cp did not shut down cleanly: the agent hung up"


def test_say_is_output_of_the_phase_with_the_guest_s_name(tmp_path: Path) -> None:
    """As a print in that phase, so an `exec` reply has it too."""
    sink = Collect()
    run = session(tmp_path, sink)
    reply = io.StringIO()
    with run._capture("cluster", copy=reply):
        run._say("cp", "[k8s] cp: control plane is up")
    [event] = sink.events
    assert (event.kind, event.phase, event.machine) == (Kind.OUTPUT, "cluster", "cp")
    assert event.text == "[k8s] cp: control plane is up"
    assert reply.getvalue() == "[k8s] cp: control plane is up\n"
