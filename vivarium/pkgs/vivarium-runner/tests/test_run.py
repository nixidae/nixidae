"""A run's guests: booted with what they require, once each; instances; teardown."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import anyio
import pytest
from vivarium_runner import MachineError
from vivarium_runner.instances import InstanceError
from vivarium_runner.run import Run, RunError, required
from vivarium_runner.testing import machine_spec

if TYPE_CHECKING:
    from vivarium_runner import Machine, MachineSpec


class FakeMachine:
    """What `Run` asks of a `Machine`, recorded."""

    def __init__(self, spec: MachineSpec, log: list[str], *, fails: bool = False, stuck: bool = False) -> None:
        self.spec = spec
        self.name = spec.name
        self.routed: tuple[Machine, ...] = ()
        self.log = log
        self.fails = fails
        self.stuck = stuck
        self.release = anyio.Event()

    def resolve_forward(self) -> None:
        pass

    async def start(self) -> None:
        self.log.append(f"start {self.name}")
        if self.stuck:
            await self.release.wait()
        if self.fails:
            raise MachineError(f"{self.name} will not boot")

    async def shutdown(self, *, kill: bool = False) -> None:
        self.log.append(f"{'kill' if kill else 'stop'} {self.name}")
        if self.fails:
            raise MachineError(f"{self.name} will not stop")


def _run(
    guests: list[MachineSpec],
    templates: list[MachineSpec] | None = None,
    *,
    fails: frozenset[str] = frozenset(),
    stuck: frozenset[str] = frozenset(),
) -> tuple[Run, list[str], dict[str, FakeMachine]]:
    log: list[str] = []
    made: dict[str, FakeMachine] = {}

    def machine(spec: MachineSpec, lan_fds: dict[str, int] | None) -> Machine:
        made[spec.name] = FakeMachine(spec, log, fails=spec.name in fails, stuck=spec.name in stuck)
        return cast("Machine", made[spec.name])

    def stop_error(name: str, error: Exception) -> None:
        log.append(f"error {name}: {error}")

    return Run(guests, templates or [], machine=machine, on_stop_error=stop_error), log, made


def test_requires_is_transitive() -> None:
    assert required(["a"], {"a": ["b"], "b": ["cache"]}) == {"a", "b", "cache"}


def test_requires_survives_a_cycle() -> None:
    assert required(["a"], {"a": ["b"], "b": ["a"]}) == {"a", "b"}


def test_a_guest_boots_with_what_it_requires_and_nothing_else() -> None:
    run, log, _ = _run([machine_spec("a", requires=("cache",)), machine_spec("cache"), machine_spec("b", index=2)])

    async def main() -> None:
        await run.prepare()
        await run.ensure(["a"])

    anyio.run(main)
    assert sorted(log) == ["start a", "start cache"]
    assert sorted(run.up) == ["a", "cache"]


def test_two_callers_boot_one_guest_once() -> None:
    run, log, made = _run([machine_spec("cache")], stuck=frozenset({"cache"}))

    async def main() -> None:
        await run.prepare()
        async with anyio.create_task_group() as group:
            group.start_soon(run.ensure, ["cache"])
            group.start_soon(run.ensure, ["cache"])
            await anyio.wait_all_tasks_blocked()
            made["cache"].release.set()

    anyio.run(main)
    assert log == ["start cache"]


def test_a_failed_boot_raises_and_its_guest_is_still_torn_down() -> None:
    run, log, _ = _run([machine_spec("a"), machine_spec("b", index=1)], fails=frozenset({"b"}))

    async def main() -> None:
        await run.prepare()
        with pytest.raises(MachineError, match="b will not boot"):
            await run.ensure(["a", "b"])
        assert list(run.up) == ["a"]
        # A second caller is told, not left waiting on the failed boot.
        with pytest.raises(MachineError, match="b did not boot"):
            await run.ensure(["b"])
        await run.teardown(kill=True)

    anyio.run(main)
    assert sorted(log[2:]) == ["error b: b will not stop", "kill a", "kill b"]


def test_an_unknown_guest_is_refused() -> None:
    run, _, _ = _run([machine_spec("a")])

    async def main() -> None:
        await run.prepare()
        with pytest.raises(RunError, match="no such guest: x; the guests are a"):
            await run.ensure(["x"])

    anyio.run(main)


def test_an_instance_takes_the_lowest_free_index_and_gives_it_back() -> None:
    run, log, _ = _run([machine_spec("a")], [machine_spec("worker", index=1)])

    async def main() -> None:
        await run.prepare()
        first = run.add("worker", "w1")
        await run.ensure(["w1"])
        assert (first.index, run.holds("w1"), run.holds("a")) == (2, True, False)
        assert run.add("worker", "w2").index == 3
        await run.terminate("w1")
        assert "w1" not in run.up
        assert not run.holds("w1")
        assert run.add("worker", "w3").index == 2
        with pytest.raises(InstanceError, match="no template 'nope'"):
            run.add("nope", "w4")
        with pytest.raises(InstanceError, match="already called 'w2'"):
            run.add("worker", "w2")
        with pytest.raises(InstanceError, match="'a' is no instance"):
            await run.terminate("a")

    anyio.run(main)
    assert log == ["start w1", "stop w1"]


def test_teardown_is_safe_twice_and_ends_the_run() -> None:
    run, log, _ = _run([machine_spec("a")])

    async def main() -> None:
        await run.prepare()
        await run.ensure(["a"])
        await run.teardown()
        await run.teardown()
        assert not run.up
        with pytest.raises(RunError, match="not prepared"):
            await run.ensure(["a"])

    anyio.run(main)
    assert log == ["start a", "stop a"]
