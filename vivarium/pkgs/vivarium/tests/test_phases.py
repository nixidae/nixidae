"""What a failure takes with it.

The rule under test: a failed phase skips what depends on it and nothing
else. Both neighbours get it wrong -- nixpkgs' driver ends the run, and
pytest would run every phase -- so the cases that matter are the ones
where those two disagree with this.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
from vivarium.phases import (
    IllegalMove,
    Move,
    Outcome,
    PhaseState,
    ReloadRefused,
    Verdict,
    breakpoints,
    dependents,
    guests,
    launchable,
    passed,
    ready,
    reloaded,
    runnable,
    selected,
    skipped_by,
    summarise,
    transition,
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


class TestSelected:
    """`--only`: the named phases and what they run after. nixidae/nixidae#661."""

    def test_the_after_closure_comes_with_it(self) -> None:
        assert selected(["check"], CHAIN) == {"boot", "cluster", "check"}

    def test_nothing_after_it_comes(self) -> None:
        assert "report" not in selected(["check"], CHAIN)

    def test_an_always_phase_after_it_stays_out(self) -> None:
        journal = phase("journal", "check", always=True)
        assert "journal" not in selected(["check"], [*CHAIN, journal])

    def test_several_names_join(self) -> None:
        assert selected(["unrelated", "cluster"], CHAIN) == {"boot", "cluster", "unrelated"}


class TestBreakpoints:
    def test_phases_and_points_come_apart(self) -> None:
        assert breakpoints(["check", "end", "failure"], CHAIN) == ({"check"}, {"end", "failure"})

    def test_a_misspelling_is_refused(self) -> None:
        with pytest.raises(ValueError, match="chek"):
            breakpoints(["chek"], CHAIN)

    def test_a_phase_named_like_a_point_is_refused(self) -> None:
        with pytest.raises(ValueError, match="clash"):
            breakpoints([], [*CHAIN, phase("end", "report")])


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


class TestGuests:
    """Which guests a phase needs up. `--only` boots these and no more."""

    BOOTS = PhaseSpec(name="boot", script=Path("/dev/null/boot.py"), nodesFromDependents=True)
    A = on("a", "boot", nodes=("a",))
    C = on("c", "boot", nodes=("c",))
    ALL = frozenset({"a", "b", "c"})

    def test_a_phase_with_nodes_needs_those(self) -> None:
        assert guests(self.A, [self.BOOTS, self.A], {}, self.ALL) == {"a"}

    def test_boot_takes_the_guests_of_what_still_runs(self) -> None:
        state = {"boot": PhaseState.PENDING, "a": PhaseState.PENDING, "c": PhaseState.DESELECTED}
        assert guests(self.BOOTS, [self.BOOTS, self.A, self.C], state, self.ALL) == {"a"}

    def test_a_full_run_boots_every_guest(self) -> None:
        """A phase may reach a peer it names nowhere."""
        state = {"boot": PhaseState.PENDING, "a": PhaseState.PENDING, "c": PhaseState.PENDING}
        assert guests(self.BOOTS, [self.BOOTS, self.A, self.C], state, self.ALL) == self.ALL

    def test_boot_alone_boots_every_guest(self) -> None:
        state = {"boot": PhaseState.PENDING, "a": PhaseState.DESELECTED, "c": PhaseState.DESELECTED}
        assert guests(self.BOOTS, [self.BOOTS, self.A, self.C], state, self.ALL) == self.ALL

    def test_a_later_phase_without_nodes_takes_every_guest(self) -> None:
        anywhere = on("anywhere", "a")
        phases = [self.BOOTS, self.A, self.C, anywhere]
        state = {"boot": PhaseState.PENDING, "a": PhaseState.PENDING, "c": PhaseState.DESELECTED}
        assert guests(self.BOOTS, phases, {**state, "anywhere": PhaseState.PENDING}, self.ALL) == self.ALL


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

    def test_an_interrupted_phase_is_not_a_pass(self) -> None:
        """`ctl stop` or a signal mid-phase: the phase has no answer."""
        assert not passed({"a": PhaseState.PASSED, "b": PhaseState.INTERRUPTED})

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

    def test_no_phases_is_not_a_pass(self) -> None:
        """A run with nothing to answer has no answer. `modules/run.nix`
        refuses such a run; this holds for a spec written by hand."""
        assert not passed({})


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


class TestReload:
    """A reload swaps the phase list under a run that has state."""

    def test_a_phase_keeps_its_state_and_a_new_one_is_pending(self) -> None:
        old = [phase("boot"), phase("check", "boot")]
        new = [
            phase("boot"),
            PhaseSpec(name="check", script=Path("/dev/null/v2.py"), after=["boot"]),
            phase("more", "check"),
        ]
        state = {"boot": PhaseState.PASSED, "check": PhaseState.FAILED}
        done = reloaded(old, new, state)
        assert done.state == {"boot": PhaseState.PASSED, "check": PhaseState.FAILED, "more": PhaseState.PENDING}
        assert (done.added, done.removed, done.changed) == (("more",), (), ("check",))
        assert done.describe() == "added more; changed check"

    def test_a_selection_does_not_grow(self) -> None:
        state = {"boot": PhaseState.PASSED, "other": PhaseState.DESELECTED}
        done = reloaded([phase("boot"), phase("other")], [phase("boot"), phase("other"), phase("new")], state)
        assert done.state["new"] is PhaseState.DESELECTED

    def test_a_phase_that_did_not_run_may_go(self) -> None:
        done = reloaded(
            [phase("boot"), phase("old")], [phase("boot")], {"boot": PhaseState.PASSED, "old": PhaseState.PENDING}
        )
        assert done.state == {"boot": PhaseState.PASSED}
        assert done.removed == ("old",)

    def test_a_phase_that_ran_must_stay(self) -> None:
        with pytest.raises(ReloadRefused, match=r"no check \(failed\)"):
            reloaded([phase("check")], [phase("other")], {"check": PhaseState.FAILED})

    def test_nothing_changed(self) -> None:
        assert reloaded([phase("a")], [phase("a")], {"a": PhaseState.PENDING}).describe() == "no phase changed"


class TestVerdict:
    STATE: ClassVar = {"boot": PhaseState.PASSED, "check": PhaseState.FAILED, "after": PhaseState.SKIPPED}

    def test_round_trip(self) -> None:
        verdict = Verdict.of(self.STATE, {"check": "boom"}, None)
        assert Verdict.from_json(verdict.to_json()) == verdict
        assert verdict.phases[1] == Outcome("check", PhaseState.FAILED, "boom")
        assert not verdict.passed

    def test_the_file_a_reader_already_parses(self) -> None:
        document = json.loads(Verdict.of({"boot": PhaseState.PASSED}, {}, None).to_json())
        assert document == {
            "passed": True,
            "stopped": None,
            "phases": [{"name": "boot", "state": "passed", "error": None}],
        }

    def test_a_stopped_run_never_passes(self) -> None:
        verdict = Verdict.of({"boot": PhaseState.PASSED}, {}, "ctl stop")
        assert (verdict.passed, verdict.stopped) == (False, "ctl stop")

    @pytest.mark.parametrize(
        "text", ["", "[]", '{"passed": true}', '{"passed": true, "stopped": null, "phases": [{}]}']
    )
    def test_anything_else_is_a_value_error(self, text: str) -> None:
        with pytest.raises(ValueError):  # noqa: PT011 -- the one error a reader catches
            Verdict.from_json(text)


class TestTransition:
    """Every move from every state, against a table written out by hand."""

    P = PhaseState
    LEGAL: ClassVar = {
        **{(state, Move.START): PhaseState.RUNNING for state in PhaseState if state is not PhaseState.RUNNING},
        (P.RUNNING, Move.PASS): P.PASSED,
        (P.RUNNING, Move.FIND_DONE): P.UNNEEDED,
        (P.RUNNING, Move.FAIL): P.FAILED,
        (P.RUNNING, Move.INTERRUPT): P.INTERRUPTED,
        (P.PENDING, Move.SKIP): P.SKIPPED,
        (P.PENDING, Move.DESELECT): P.DESELECTED,
    }

    @pytest.mark.parametrize("state", list(PhaseState))
    @pytest.mark.parametrize("move", list(Move))
    def test_each_pair(self, state: PhaseState, move: Move) -> None:
        if (state, move) in self.LEGAL:
            assert transition(state, move) is self.LEGAL[state, move]
        else:
            with pytest.raises(IllegalMove):
                transition(state, move)

    def test_a_running_phase_cannot_start_again(self) -> None:
        with pytest.raises(IllegalMove, match="a running phase cannot start"):
            transition(PhaseState.RUNNING, Move.START)

    @pytest.mark.parametrize("state", [PhaseState.PASSED, PhaseState.FAILED, PhaseState.SKIPPED])
    def test_ctl_run_runs_a_phase_again_whatever_its_verdict(self, state: PhaseState) -> None:
        assert transition(state, Move.START) is PhaseState.RUNNING

    def test_a_skip_never_overwrites_a_verdict(self) -> None:
        with pytest.raises(IllegalMove):
            transition(PhaseState.PASSED, Move.SKIP)
