"""What a failure takes with it.

The rule under test: a failed phase skips what depends on it and nothing
else. Both neighbours get it wrong -- nixpkgs' driver ends the run, and
pytest would run every phase -- so the cases that matter are the ones
where those two disagree with this.
"""

from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from vivarium.phases import (
    PhaseState,
    dependents,
    launchable,
    passed,
    ready,
    runnable,
    skipped_by,
    summarise,
)
from vivarium.spec import PhaseSpec, PytestSpec


def phase(name: str, *after: str, always: bool = False) -> PhaseSpec:
    return PhaseSpec(
        name=name,
        script=Path(f"/dev/null/{name}.py"),
        after=list(after),
        always=always,
    )


BOOT = phase("boot")
CLUSTER = phase("cluster", "boot")
CHECK = phase("check", "cluster")
REPORT = phase("report", "check")
UNRELATED = phase("unrelated", "boot")

CHAIN = [BOOT, CLUSTER, CHECK, REPORT, UNRELATED]


class TestDependents:
    def test_a_direct_dependent_is_found(self) -> None:
        assert dependents("cluster", CHAIN) == {"check", "report"}

    def test_it_is_transitive(self) -> None:
        """`report` needs `check` needs `cluster`.

        A direct-only answer runs `report` against a cluster that was
        never built, which fails for a reason that is not the reason.
        """
        assert "report" in dependents("cluster", CHAIN)

    def test_a_phase_is_not_its_own_dependent(self) -> None:
        assert "cluster" not in dependents("cluster", CHAIN)

    def test_an_unrelated_branch_is_untouched(self) -> None:
        assert "unrelated" not in dependents("cluster", CHAIN)

    def test_a_leaf_takes_nothing_with_it(self) -> None:
        assert dependents("report", CHAIN) == set()

    def test_the_answer_does_not_depend_on_input_order(self) -> None:
        """The sort happens in Nix. A function that quietly needs its
        input sorted is a trap for whoever changes the sort."""
        assert dependents("cluster", list(reversed(CHAIN))) == dependents("cluster", CHAIN)


class TestSkippedBy:
    def test_the_independent_phase_still_runs(self) -> None:
        """The whole reason this is not nixpkgs' answer.

        nixpkgs re-raises out of `subtest`, so `unrelated` never runs and
        a real bug in it is invisible until `cluster` is fixed.
        """
        assert "unrelated" not in skipped_by("cluster", CHAIN)

    def test_the_dependents_do_not(self) -> None:
        """And not pytest's answer either: `check` against a cluster that
        does not exist is a second failure that says nothing."""
        assert skipped_by("cluster", CHAIN) == {"check", "report"}


class TestAlways:
    """A phase that collects evidence must survive the failure.

    `after` normally means "run me later" and "do not bother if that
    failed" at once. A journal wants only the first: it is most wanted on
    the run where something broke, and ordering it after everything would
    otherwise have the failure skip it.
    """

    JOURNAL = phase("journal", "cluster", always=True)
    AFTER_JOURNAL = phase("summary", "journal")
    WITH_JOURNAL: ClassVar[list[PhaseSpec]] = [BOOT, CLUSTER, JOURNAL, AFTER_JOURNAL]

    def test_an_always_phase_is_not_skipped(self) -> None:
        assert "journal" not in skipped_by("cluster", self.WITH_JOURNAL)

    def test_a_failure_does_not_pass_through_it(self) -> None:
        """`summary` is after `journal`, which ran. So it runs too.

        Without this, adding a journal to the end of a run would silently
        skip everything a consumer put after it, any time anything failed.
        """
        assert "summary" not in skipped_by("cluster", self.WITH_JOURNAL)

    def test_an_ordinary_dependent_is_still_skipped(self) -> None:
        assert skipped_by("boot", [BOOT, CLUSTER, self.JOURNAL]) == {"cluster"}


class TestRunnable:
    def test_only_pending_phases_are_offered(self) -> None:
        state = {
            "boot": PhaseState.PASSED,
            "cluster": PhaseState.FAILED,
            "check": PhaseState.SKIPPED,
            "report": PhaseState.SKIPPED,
            "unrelated": PhaseState.PENDING,
        }
        assert [p.name for p in runnable(CHAIN, state)] == ["unrelated"]

    def test_an_unknown_phase_counts_as_pending(self) -> None:
        assert len(runnable(CHAIN, {})) == len(CHAIN)

    def test_the_order_given_is_the_order_returned(self) -> None:
        """Nix sorted them. Re-sorting here would be a second opinion."""
        assert [p.name for p in runnable(CHAIN, {})] == [p.name for p in CHAIN]


def on(name: str, *after: str, nodes: tuple[str, ...] = (), pytest_: bool = False) -> PhaseSpec:
    if pytest_:
        return PhaseSpec(
            name=name,
            after=list(after),
            nodes=list(nodes),
            pytest=PytestSpec(tests=Path("/dev/null/t")),
        )
    return PhaseSpec(name=name, after=list(after), nodes=list(nodes), script=Path(f"/dev/null/{name}.py"))


EVERY = frozenset({"unit", "protocol", "parity"})
PREPARE = on("prepare")
SUITES = [on(name, "prepare", nodes=(name,)) for name in ("unit", "protocol", "parity")]
LEAKS = on("leaks", "unit", "protocol", "parity")
PYNIXD = [PREPARE, *SUITES, LEAKS]


def names(phases: list[PhaseSpec]) -> list[str]:
    return [phase.name for phase in phases]


class TestReady:
    def test_a_phase_waits_for_its_after(self) -> None:
        assert names(ready(PYNIXD, {})) == ["prepare"]

    def test_every_independent_phase_is_ready_at_once(self) -> None:
        assert names(ready(PYNIXD, {"prepare": PhaseState.PASSED})) == ["unit", "protocol", "parity"]

    def test_a_running_dependency_is_not_an_answer(self) -> None:
        state = {"prepare": PhaseState.PASSED, "unit": PhaseState.RUNNING, "protocol": PhaseState.PASSED}
        assert "leaks" not in names(ready(PYNIXD, state))

    def test_a_failed_dependency_is_an_answer(self) -> None:
        """For what the failure left pending: an `always` phase here."""
        state = {
            "prepare": PhaseState.PASSED,
            "unit": PhaseState.FAILED,
            "protocol": PhaseState.PASSED,
            "parity": PhaseState.PASSED,
        }
        assert names(ready(PYNIXD, state)) == ["leaks"]

    def test_a_deselected_dependency_is_an_answer(self) -> None:
        """`--only unit` runs `unit` though `prepare` never ran."""
        state = dict.fromkeys(names(PYNIXD), PhaseState.DESELECTED) | {"unit": PhaseState.PENDING}
        assert names(ready(PYNIXD, state)) == ["unit"]


class TestLaunchable:
    def test_disjoint_guests_run_together(self) -> None:
        assert names(launchable(SUITES, [], EVERY)) == ["unit", "protocol", "parity"]

    def test_a_phase_without_nodes_holds_every_guest(self) -> None:
        """Why a session that declares nothing still runs one phase at a time."""
        assert names(launchable([PREPARE, *SUITES], [], EVERY)) == ["prepare"]
        assert launchable(SUITES, [PREPARE], EVERY) == []

    def test_nothing_starts_beside_an_all_guest_phase(self) -> None:
        assert launchable([LEAKS], [SUITES[0]], EVERY) == []

    def test_a_shared_guest_waits(self) -> None:
        both = on("both", nodes=("unit", "parity"))
        assert names(launchable([both, SUITES[1]], [SUITES[0]], EVERY)) == ["protocol"]

    def test_a_pytest_phase_runs_alone(self) -> None:
        """pytest.main is not reentrant, and its stdout capture is the
        process's. Disjoint guests change neither."""
        a = on("a", nodes=("unit",), pytest_=True)
        b = on("b", nodes=("parity",), pytest_=True)
        assert names(launchable([a, b], [], EVERY)) == ["a"]
        assert launchable([b, SUITES[1]], [a], EVERY) == []
        assert launchable([a], [SUITES[1]], EVERY) == []

    def test_the_order_given_decides_a_collision(self) -> None:
        first = on("first", nodes=("unit",))
        second = on("second", nodes=("unit",))
        assert names(launchable([first, second], [], EVERY)) == ["first"]


class TestPassed:
    def test_all_passed_is_a_pass(self) -> None:
        assert passed(dict.fromkeys(("a", "b"), PhaseState.PASSED))

    def test_a_failure_is_not(self) -> None:
        assert not passed({"a": PhaseState.PASSED, "b": PhaseState.FAILED})

    def test_a_skip_is_not_a_pass(self) -> None:
        """The case that decides whether this rule is safe at all.

        Skipping on failure only beats stopping if a skipped phase still
        fails the run. Otherwise a failure plus its dependents reads as
        green, and the runner reports success holding none of the answers
        it was asked for.
        """
        assert not passed({"a": PhaseState.PASSED, "b": PhaseState.SKIPPED})

    def test_a_pending_phase_is_not_a_pass(self) -> None:
        assert not passed({"a": PhaseState.PENDING})

    def test_a_deselected_phase_is_a_pass(self) -> None:
        """`--only mine` must exit 0 when `mine` passed.

        The opposite of the case above, and the reason the two states are
        not one. A developer who asked for one phase knows the rest did
        not run. Nothing in a sandbox can deselect, so this cannot make a
        check green.
        """
        assert passed({"a": PhaseState.PASSED, "b": PhaseState.DESELECTED})

    def test_deselecting_does_not_excuse_a_failure(self) -> None:
        assert not passed({"a": PhaseState.FAILED, "b": PhaseState.DESELECTED})

    def test_deselecting_everything_is_not_a_failure(self) -> None:
        assert passed({"a": PhaseState.DESELECTED})


class TestSummarise:
    def test_it_counts_each_outcome(self) -> None:
        line = summarise(
            {
                "a": PhaseState.PASSED,
                "b": PhaseState.PASSED,
                "c": PhaseState.FAILED,
                "d": PhaseState.SKIPPED,
            }
        )
        assert line == "2 passed, 1 failed, 1 skipped"

    def test_an_empty_run_says_nothing_rather_than_breaking(self) -> None:
        assert summarise({}) == ""


class TestPhaseStateIsAString:
    """It crosses a boundary: a report, an MCP reply, a log line."""

    def test_it_serialises_as_its_own_name(self) -> None:
        assert PhaseState.SKIPPED == "skipped"

    @pytest.mark.parametrize("state", list(PhaseState))
    def test_every_member_reads_as_a_word(self, state: PhaseState) -> None:
        assert state.value.isalpha()
