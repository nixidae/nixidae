"""`Machine.until`, `never`, `wait_until_fails` and `Machines.until`, on machines with no guest."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping

import anyio
import pytest
from vivarium_runner import Machine, Machines, report
from vivarium_runner.machine import MachineError

FAST = 0.01


class Polled(Machine):
    """A machine whose state is a list of answers, one per check; the last repeats."""

    def __init__(self, name: str, answers: list[object]) -> None:
        self._polled_name = name
        self.answers = answers
        self.asked = 0
        self.recorder = report.Report()

    @property
    def name(self) -> str:
        return self._polled_name

    async def state(self) -> object:
        answer = self.answers[min(self.asked, len(self.answers) - 1)]
        self.asked += 1
        return answer

    async def execute(
        self,
        command: str,
        timeout: float | None = None,
        label: str | None = None,
        env: Mapping[str, str] | None = None,
    ) -> tuple[int, str]:
        rc = await self.state()
        if not isinstance(rc, int):
            raise TypeError(rc)
        return rc, f"rc {rc}"


def run[T](main: Callable[[], Awaitable[T]]) -> T:
    return anyio.run(main)


def test_until_returns_the_first_answer_that_is_not_none() -> None:
    vm = Polled("one", [None, None, "up"])

    async def main() -> object:
        return await vm.until("up", vm.state, timeout=5, poll=FAST)

    assert run(main) == "up"
    assert vm.asked == 3


@pytest.mark.parametrize("answer", [False, {}, 0, ""])
def test_a_falsy_answer_is_a_result(answer: object) -> None:
    vm = Polled("one", [answer])

    async def main() -> object:
        return await vm.until("up", vm.state, timeout=5, poll=FAST)

    assert run(main) == answer
    assert vm.asked == 1


def test_until_names_the_guest_and_the_evidence_at_the_deadline() -> None:
    vm = Polled("one", [None])

    async def evidence() -> str:
        return "peer 10.0.0.1: Active"

    async def main() -> object:
        return await vm.until("BGP up", vm.state, timeout=0.05, poll=FAST, evidence=evidence)

    with pytest.raises(MachineError, match=r"\[one\] BGP up: not reached within 0s:\npeer 10.0.0.1: Active"):
        run(main)


def test_the_deadline_bounds_a_slow_check() -> None:
    vm = Polled("one", [None])

    async def slow() -> None:
        await anyio.sleep(10)

    async def main() -> None:
        await vm.until("slow", slow, timeout=0.05, poll=FAST)

    started = time.monotonic()
    with pytest.raises(MachineError, match="not reached"):
        run(main)
    assert time.monotonic() - started < 5


def test_until_is_one_wait_step() -> None:
    vm = Polled("one", [None, "up"])

    async def main() -> object:
        return await vm.until("the route", vm.state, timeout=5, poll=FAST)

    run(main)
    assert [(s.machine, s.kind, s.what) for s in vm.recorder.steps] == [("one", "wait", "the route")]


def test_never_holds_for_the_whole_timeout() -> None:
    vm = Polled("one", [None])

    async def main() -> None:
        await vm.never("a route back", vm.state, timeout=0.05, poll=FAST)

    run(main)
    assert vm.asked > 1


def test_never_raises_with_what_it_saw() -> None:
    vm = Polled("one", [None, "10.2.0.0/24 via ipsec1"])

    async def main() -> None:
        await vm.never("a route back", vm.state, timeout=5, poll=FAST)

    with pytest.raises(MachineError, match=r"\[one\] a route back: happened after 0s:\n10.2.0.0/24 via ipsec1"):
        run(main)


def test_wait_until_fails_returns_the_failing_output() -> None:
    vm = Polled("one", [0, 0, 1])

    async def main() -> str:
        return await vm.wait_until_fails("ping -c 1 10.0.0.1", timeout=5, poll=FAST)

    assert run(main) == "rc 1"


def test_machines_until_names_every_guest_that_failed() -> None:
    vms = Machines((vm.name, vm) for vm in (Polled("a", ["up"]), Polled("b", [None]), Polled("c", [None])))

    async def check(vm: Machine) -> object:
        if not isinstance(vm, Polled):
            raise TypeError(vm)
        return await vm.state()

    async def evidence(vm: Machine) -> str:
        return f"{vm.name}: peer down"

    async def main() -> object:
        return await vms.until("BGP up", check, timeout=0.05, poll=FAST, evidence=evidence)

    with pytest.raises(MachineError, match=r"^BGP up: failed on b, c\n\[b\] BGP up") as raised:
        run(main)
    assert "[c] BGP up: not reached within 0s:\nc: peer down" in str(raised.value)


def test_machines_until_returns_each_result_and_takes_a_subset() -> None:
    vms = Machines((vm.name, vm) for vm in (Polled("a", [None, 1]), Polled("b", [2]), Polled("c", [None])))

    async def check(vm: Machine) -> object:
        if not isinstance(vm, Polled):
            raise TypeError(vm)
        return await vm.state()

    async def main() -> object:
        return await vms.until("up", check, timeout=5, poll=FAST, machines=["a", "b"])

    assert run(main) == {"a": 1, "b": 2}
