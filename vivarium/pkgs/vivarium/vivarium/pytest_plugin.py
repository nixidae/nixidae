"""pytest as a phase.

A phase declared with `pytest.tests` instead of `script` is a pytest run
against the session's guests, which are already up. What a test author
gets is pytest -- assertion rewriting, `-k`, parametrize, fixtures,
`conftest.py` -- and the same `Machine` API a phase script uses:

    async def test_hostname(one: Machine) -> None:
        assert (await one.succeed("hostname")).stdout == "one"

**pytest runs in a worker thread, and the guests stay on the session's
loop.** A `Machine` belongs to the loop that started it, so an async
test or fixture is sent back to that loop through a `BlockingPortal`
and awaited there. The loop stays free between calls, which is why the
journal keeps streaming while a test runs.

It is a phase and not the owner of the run. Ordering lives in Nix,
where a failure skips what depends on it; pytest orders nothing and
would run a check against a cluster that was never built.

Each guest is a fixture named after it, and `vms` is all of them.
"""

from __future__ import annotations

import functools
import inspect
import math
import types
from collections import Counter
from typing import TYPE_CHECKING, Any, Literal

import anyio
import pytest

from .async_fixtures import bound, in_loop, is_async
from .events import Kind, Level
from .phases import PhaseTimeout

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    from anyio.from_thread import BlockingPortal
    from vivarium_runner import Machine, Machines

    from .session import Session

type Outcome = Literal["passed", "failed", "skipped", "error"]


class MachineFixtures(types.ModuleType):
    """The module that holds the fixtures.

    `vms` is set to a fixture that pytest finds by name, so its type is
    pytest's own and not one this package defines.
    """

    vms: object


def machine_fixtures(vms: Machines) -> MachineFixtures:
    """One fixture per guest, named after it.

    A module and not a class: pytest binds a fixture found on an
    instance as a method, and these take no `self`.
    """
    module = MachineFixtures("uml_machines")
    for name in vms:

        def make(name: str = name) -> Callable[[], Machine]:
            @pytest.fixture(name=name)
            def machine() -> Machine:
                return vms[name]

            return machine

        setattr(module, f"_machine_{name}", make())

    @pytest.fixture(name="vms")
    def all_of_them() -> Machines:
        return vms

    module.vms = all_of_them
    return module


class Plugin:
    """The hooks that tie one pytest run to one session."""

    def __init__(
        self,
        session: Session,
        phase: str,
        portal: BlockingPortal,
        *,
        by_hand: bool = False,
        deadline: float | None = None,
        timeout: float | None = None,
        on_case: Callable[[str], None] | None = None,
    ) -> None:
        self.session = session
        self.on_case = on_case
        self.phase = phase
        self.portal = portal
        self.marks: dict[str, Any] = {"by_hand": True} if by_hand else {}
        self.outcomes: Counter[Outcome] = Counter()
        self.deadline = math.inf if deadline is None else deadline
        """On the loop's clock, `anyio.current_time()`."""
        self.timeout = timeout
        self.timed_out: PhaseTimeout | None = None

    async def _bounded(self, step: Callable[[], Any]) -> Any:
        """*step* on the loop, cancelled at the phase's deadline.

        Each test and async fixture gets the same deadline, not a timeout
        of its own: the limit is the phase's.
        """
        with anyio.CancelScope(deadline=self.deadline):
            return await step()
        # Only this scope's own deadline gets here: a cancel from outside
        # propagates out of the `with`.
        self.timed_out = self._timeout()
        raise self.timed_out

    def _expire(self) -> PhaseTimeout:
        self.timed_out = self._timeout()
        return self.timed_out

    def _timeout(self) -> PhaseTimeout:
        return PhaseTimeout(f"phase {self.phase} ran past its timeout of {self.timeout:g}s")

    def _emit(self, kind: Kind, text: str, **fields: Any) -> None:
        # On the loop, never from this thread: the sinks write files and
        # the follower writes the same files from the loop.
        self.portal.call(functools.partial(self.session.emit, kind, text, phase=self.phase, **self.marks, **fields))

    # ── running async code on the session's loop ───────────────────

    @pytest.hookimpl(tryfirst=True)
    def pytest_pyfunc_call(self, pyfuncitem: pytest.Function) -> bool | None:
        test = pyfuncitem.obj
        if not inspect.iscoroutinefunction(test):
            return None
        names = pyfuncitem._fixtureinfo.argnames
        kwargs = {name: pyfuncitem.funcargs[name] for name in names}
        self.portal.call(self._bounded, functools.partial(test, **kwargs))
        return True

    def pytest_runtest_teardown(self, item: pytest.Item) -> None:
        # A sync test, or a blocking call, can run past the deadline with
        # no await to see it: the clock fails the phase after it ends.
        if self.timed_out is None and self.portal.call(anyio.current_time) >= self.deadline:
            self.timed_out = self._timeout()
        # Past the deadline every test would fail at its first await, so
        # pytest starts no other.
        if self.timed_out is not None:
            item.session.shouldstop = str(self.timed_out)

    @pytest.hookimpl(hookwrapper=True, tryfirst=True)
    def pytest_fixture_setup(
        self, fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest
    ) -> Generator[None]:
        func = fixturedef.func
        if not is_async(func):
            yield
            return
        stand_in = in_loop(self.portal, bound(func, request), deadline=self.deadline, expired=self._expire)
        fixturedef.func = stand_in  # type: ignore[misc]  # pytest declares it Final; anyio's plugin swaps it too
        try:
            yield
        finally:
            # The definition is shared by every test that asks for it, so it goes back.
            fixturedef.func = func  # type: ignore[misc]  # Final in pytest, as above

    # ── which test is running, for everything else that happens ────

    def pytest_runtest_logstart(self, nodeid: str, location: object) -> None:
        self.portal.call(self.session.begin_case, nodeid)

    def pytest_runtest_logfinish(self, nodeid: str, location: object) -> None:
        self.portal.call(self.session.end_case)

    # ── outcomes ────────────────────────────────────────────────────

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        for title, content in report.sections:
            if title == f"Captured stdout {report.when}":
                for line in content.splitlines():
                    if line.strip():
                        self._emit(Kind.OUTPUT, line, case=report.nodeid)

        outcome: Outcome
        if report.when == "call":
            outcome = report.outcome
        elif report.failed:
            # A fixture that failed: pytest calls that an error, not a
            # failure, and so does a JUnit reader.
            outcome = "error"
        elif report.skipped and report.when == "setup":
            outcome = "skipped"
        else:
            return
        self._case(report.nodeid, outcome, report.when, report.duration, report)

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            self._case(report.nodeid or self.phase, "error", "collect", 0.0, report)

    def _case(
        self,
        nodeid: str,
        outcome: Outcome,
        when: str,
        seconds: float,
        report: pytest.TestReport | pytest.CollectReport,
    ) -> None:
        self.outcomes[outcome] += 1
        fields: dict[str, Any] = {"outcome": outcome, "when": when}
        bad = outcome in ("failed", "error")
        if bad:
            fields["error"] = report.longreprtext
            crash = getattr(report.longrepr, "reprcrash", None)
            if crash is not None:
                fields["message"] = crash.message
        elif outcome == "skipped" and isinstance(report.longrepr, tuple):
            fields["reason"] = str(report.longrepr[2])
        self._emit(
            Kind.CASE,
            nodeid,
            level=Level.ERROR if bad else Level.INFO,
            seconds=seconds,
            **fields,
        )
        if bad:
            self._emit(Kind.ERROR, report.longreprtext, level=Level.ERROR, case=nodeid)
        if self.on_case is not None:
            self.portal.call(self.on_case, case_line(nodeid, outcome, seconds, fields.get("message")))

    def summary(self) -> str:
        """`2 passed, 1 failed`, in pytest's own order."""
        order: tuple[Outcome, ...] = ("failed", "error", "passed", "skipped")
        return ", ".join(f"{self.outcomes[name]} {name}" for name in order if self.outcomes[name]) or "no tests ran"


def case_line(nodeid: str, outcome: str, seconds: float, message: str | None = None) -> str:
    """One case as a streaming client prints it: `failed tests/x.py::t (1.2s): boom`."""
    tail = f": {message.strip().splitlines()[0]}" if message and message.strip() else ""
    return f"{outcome} {nodeid} ({seconds:.1f}s){tail}\n"


def arguments(tests: str, extra: list[str]) -> list[str]:
    """What `pytest.main` is given, before the caller's own arguments.

    Every choice here is about sharing a process with a running session:

    - `no:terminal`: every outcome is already an event, and the terminal
      sink prints those. pytest's own report would be the same run twice.
    - `--capture=sys`: `fd` capture swaps file descriptor 1 while a test
      runs, and the loop thread's terminal writes to it at the same time.
    - `no:cacheprovider`: the tests are in the store, which is read-only.
    - `--import-mode=importlib`: nothing is put on `sys.path`, so two
      pytest phases with a `test_basic.py` each do not collide.
    - `no:vivarium`: `vivarium.pytest_host` drives runs from outside one,
      and its `vms` would stand in for this run's guests.
    """
    return [
        tests,
        "-p",
        "no:terminal",
        "-p",
        "no:vivarium",
        "-p",
        "no:cacheprovider",
        "--capture=sys",
        "--import-mode=importlib",
        *extra,
    ]
