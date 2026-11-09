"""Which phases run, and which a failure takes with it.

All of it is a function of the phase list and what has happened so far,
so all of it is a function.  The session applies the answers; nothing
here touches a guest, a file or a clock.

The rule this exists for: **a failed phase skips what depends on it and
nothing else.**  nixpkgs' driver stops the whole run -- a `subtest` logs
and re-raises -- and pytest runs every test whatever happened.  Phases
are neither.  Running `check` after `cluster` failed produces a second
failure that says nothing, while an unrelated phase that would have found
a real bug never runs at all.

Knowing which is which needs the dependency graph, which is why `after`
is a list of names and not an order number.  See
`docs/design/history/running-anywhere.md`, area 0e.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .spec import PhaseSpec


class PhaseState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"

    SKIPPED = "skipped"
    """Its dependency failed, so nobody knows what it would have done.
    A run holding one of these did not pass."""

    INTERRUPTED = "interrupted"
    """It was running when the run was stopped -- ^C, a signal, or
    `ctl stop`. Without this it stayed `running` in `phases.json` for
    good, which reads as a run still in progress."""

    DESELECTED = "deselected"
    """The caller asked for other phases by name.

    Kept apart from `SKIPPED` because the two mean opposite things to a
    reader and to an exit code. A developer running `--only mine` knows
    the rest did not run and wants exit 0 when theirs passed; a run whose
    dependency failed must not report success. Nothing in a sandbox can
    produce this -- the check passes no `--only` -- so CI cannot go green
    by running a subset."""


STOPPED: Final = 5
"""The exit status of a run stopped by `ctl stop`, and of a monitor that
sees one: not a pass, and not a failure either."""


@dataclass(frozen=True, slots=True)
class Stop:
    """Why a run ended before its verdict, and the status it exits with."""

    reason: str
    status: int

    @classmethod
    def by_request(cls) -> Stop:
        return cls("ctl stop", STOPPED)

    @classmethod
    def by_signal(cls, number: int, name: str) -> Stop:
        return cls(name, 128 + number)


class PhaseTimeout(TimeoutError):
    """A phase ran past its `timeout`. Not a bare TimeoutError: a command
    in the phase that times out raises that, and is a different failure."""


def dependents(name: str, phases: Iterable[PhaseSpec]) -> set[str]:
    """Every phase that depends on *name*, directly or through another.

    Transitive on purpose.  `check` after `cluster` is obvious; `report`
    after `check` after `cluster` is the one a direct-only answer runs
    anyway, against a cluster that was never built.
    """
    phases = list(phases)
    found = {name}
    # Repeat until nothing new appears. The list is sorted, so one pass
    # would be enough -- but the sort is done elsewhere, and a function
    # that quietly depends on its input's order is a trap for whoever
    # changes the sort.
    while True:
        grown = found | {
            phase.name
            for phase in phases
            # `always` stops the walk as well as excluding itself. A phase
            # that runs whatever happened does not pass the failure on, so
            # nothing after it is skipped on account of something before
            # it.
            if not phase.always and found & set(phase.after)
        }
        if grown == found:
            return found - {name}
        found = grown


def selected(names: Iterable[str], phases: Iterable[PhaseSpec]) -> set[str]:
    """What `--only` runs: *names* and every phase they run after, transitively.

    Without the `after` closure, `--only check` ran `check` on a guest that
    `cluster` never set up (nixidae/nixidae#661). Only upward: an `always`
    phase after *names* stays deselected, because the caller did not ask
    for it.
    """
    by_name = {phase.name: phase for phase in phases}
    found: set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        if name in found:
            continue
        found.add(name)
        todo.extend(by_name[name].after if name in by_name else ())
    return found


START = "start"
FAILURE = "failure"
END = "end"
POINTS = frozenset({START, FAILURE, END})
"""What `--break` takes beside a phase name: before the first phase, at a
failure, after the last phase."""


def breakpoints(names: Iterable[str], phases: Iterable[PhaseSpec]) -> tuple[set[str], set[str]]:
    """`--break` arguments, as the phases to pause before and the points.

    A phase named like a point is refused rather than guessed at: either
    reading of `--break end` would surprise somebody.
    """
    have = {phase.name for phase in phases}
    clash = POINTS & have
    if clash:
        raise ValueError(f"phases named {', '.join(sorted(clash))} clash with `--break` points of the same name")
    names = set(names)
    unknown = names - have - POINTS
    if unknown:
        # A misspelled breakpoint is a run that never stops.
        raise ValueError(
            f"no such phase to break before: {', '.join(sorted(unknown))};"
            f" have {', '.join(sorted(have))}, or one of {', '.join(sorted(POINTS))}"
        )
    return names - POINTS, names & POINTS


def skipped_by(failed: str, phases: Iterable[PhaseSpec]) -> set[str]:
    """What a failure of *failed* means for the phases not yet run."""
    return dependents(failed, phases)


def runnable(phases: Iterable[PhaseSpec], state: dict[str, PhaseState]) -> list[PhaseSpec]:
    """The phases still worth running, in the order given."""
    return [phase for phase in phases if state.get(phase.name, PhaseState.PENDING) is PhaseState.PENDING]


UNFINISHED = frozenset({PhaseState.PENDING, PhaseState.RUNNING})


def ready(phases: Iterable[PhaseSpec], state: dict[str, PhaseState]) -> list[PhaseSpec]:
    """Pending phases whose every `after` has an answer, in the order given.

    Any answer, not only a pass. A failure has already marked what it
    takes with it `SKIPPED`, so a phase still pending here is one the
    failure does not reach: an `always` phase.
    """
    return [
        phase
        for phase in runnable(phases, state)
        if all(state.get(name, PhaseState.PENDING) not in UNFINISHED for name in phase.after)
    ]


def claims(phase: PhaseSpec, every: frozenset[str]) -> frozenset[str]:
    """The guests a phase holds while it runs. No `nodes` is all of them.

    A pytest phase holds all of them whatever its `nodes`, so it runs
    alone. `pytest.main` is not reentrant in one process, and
    `--capture=sys` swaps the process's stdout for each test: a script
    phase printing beside it would have its lines filed under that test.
    """
    if phase.pytest is not None or not phase.nodes:
        return every
    return frozenset(phase.nodes)


def guests(
    phase: PhaseSpec,
    phases: Iterable[PhaseSpec],
    state: dict[str, PhaseState],
    every: frozenset[str],
) -> frozenset[str]:
    """The guests that must be up before *phase* starts.

    A phase with `nodesFromDependents` (the `boot` recipe) takes the
    guests of every phase after it that still may run. So `--only check`
    on `nodes = [ "a" ]` boots `a` and not the rest, though every
    consumer writes `after = [ "boot" ]`.

    Only when `--only` deselected something. A full run boots every
    guest, because a phase may reach a peer it names in neither `nodes`
    nor `requires`: a client that pings a server. With no phase after it
    to run, it takes every guest too: a run of `boot` alone boots.
    """
    if not phase.nodesFromDependents or PhaseState.DESELECTED not in state.values():
        return claims(phase, every)
    phases = list(phases)
    later: set[str] = set()
    found = {phase.name}
    while grown := {p.name for p in phases if p.name not in found and found & set(p.after)}:
        found |= grown
        later |= grown
    wanted: set[str] = set()
    for other in phases:
        if other.name in later and state.get(other.name, PhaseState.PENDING) is not PhaseState.DESELECTED:
            wanted |= guests(other, phases, state, every)
    return frozenset(wanted) or every


def required(names: Iterable[str], requires: dict[str, list[str]]) -> frozenset[str]:
    """*names* and every guest they name in `vivarium.requires`, transitively."""
    found: set[str] = set()
    todo = list(names)
    while todo:
        name = todo.pop()
        if name not in found:
            found.add(name)
            todo.extend(requires.get(name, ()))
    return frozenset(found)


def launchable(
    ready: Iterable[PhaseSpec],
    running: Iterable[PhaseSpec],
    every: frozenset[str],
) -> list[PhaseSpec]:
    """What may start now beside `running`, taken in order.

    Two phases share a guest never: a phase's commands and the guest's
    state are its own while it runs, so a second phase on the same guest
    is a race nobody declared. A phase without `nodes` holds every guest,
    which is how a session that declares nothing keeps running one phase
    at a time.
    """
    held: set[str] = set()
    for phase in running:
        held |= claims(phase, every)
    chosen: list[PhaseSpec] = []
    for phase in ready:
        wants = claims(phase, every)
        if held & wants:
            continue
        held |= wants
        chosen.append(phase)
    return chosen


def passed(state: dict[str, PhaseState]) -> bool:
    """Did the run succeed?

    A skipped phase is not a pass.  It is a phase whose answer nobody
    has, and a run that reports success while holding none of the
    answers it was asked for is the failure mode this guards.

    A **deselected** phase is different: the caller said not to run it,
    so its answer was never wanted.  Only a caller can deselect, and a
    sandboxed check never does, so this cannot make CI green.
    """
    return all(value in (PhaseState.PASSED, PhaseState.DESELECTED) for value in state.values())


class ReloadRefused(ValueError):
    """A new spec that a reload cannot take into the running session."""


@dataclass(frozen=True, slots=True)
class Reloaded:
    """What a reload changes: the new state, and the phases by change."""

    state: dict[str, PhaseState]
    added: tuple[str, ...]
    removed: tuple[str, ...]
    changed: tuple[str, ...]

    def describe(self) -> str:
        groups = (("added", self.added), ("removed", self.removed), ("changed", self.changed))
        return "; ".join(f"{what} {', '.join(names)}" for what, names in groups if names) or "no phase changed"


def reloaded(old: list[PhaseSpec], new: list[PhaseSpec], state: dict[str, PhaseState]) -> Reloaded:
    """The state after the phases `old` become `new`.

    A phase keeps its state, so a phase that ran keeps its verdict until
    it is run again. A new phase is pending, or deselected when the run
    has `--only`: a reload does not grow a selection. A phase that ran
    must stay, because its verdict is part of the run's. Raises
    `ReloadRefused`."""
    names = {phase.name for phase in new}
    unrun = (PhaseState.PENDING, PhaseState.DESELECTED)
    ran = [name for name, value in state.items() if name not in names and value not in unrun]
    if ran:
        missing = ", ".join(f"{name} ({state[name]})" for name in ran)
        raise ReloadRefused(f"a reload keeps each phase that ran; the new spec has no {missing}")
    fresh = PhaseState.DESELECTED if PhaseState.DESELECTED in state.values() else PhaseState.PENDING
    before = {phase.name: phase for phase in old}
    return Reloaded(
        state={phase.name: state.get(phase.name, fresh) for phase in new},
        added=tuple(phase.name for phase in new if phase.name not in before),
        removed=tuple(name for name in before if name not in names),
        changed=tuple(phase.name for phase in new if phase.name in before and before[phase.name] != phase),
    )


def summarise(state: dict[str, PhaseState]) -> str:
    """One line per outcome, for the end of a run."""
    counts: dict[PhaseState, int] = {}
    for value in state.values():
        counts[value] = counts.get(value, 0) + 1
    return ", ".join(f"{counts[key]} {key}" for key in PhaseState if key in counts)
