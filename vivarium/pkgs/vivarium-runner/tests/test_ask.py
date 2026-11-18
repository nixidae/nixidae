"""`Machine._ask`: a timeout in the guest against a guest that does not answer."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import anyio
import pytest
from vivarium_agent import arpyc
from vivarium_runner import GuestTimeout, Machine, report
from vivarium_runner.consolelines import ConsoleLines
from vivarium_runner.machine import MachineError


class Asked(Machine):
    """A machine with no guest: `_ask` alone, with each call's event kept."""

    def __init__(self) -> None:
        self.recorder = report.Report()
        self._console = ConsoleLines()
        self._console.history.append("last console line")
        self.events: list[dict[str, object]] = []
        self.on_command = lambda _name, _what, _took, data: self.events.append(data)

    @property
    def name(self) -> str:
        return "one"


def run[T](main: Callable[[], Awaitable[T]]) -> T:
    return anyio.run(main)


GUEST_SAID = "ran past its 1s timeout; its process group got SIGKILL\npartial"


def test_a_timeout_raised_in_the_guest_is_a_guest_timeout() -> None:
    vm = Asked()

    async def call() -> None:
        # As the channel rebuilds it: its `str` is no longer the message.
        try:
            raise TimeoutError(GUEST_SAID)
        except TimeoutError as error:
            raise arpyc._load_exc(arpyc._dump_exc(error)) from None

    async def main() -> None:
        await vm._ask("sleep 5", call(), timeout=5)

    with pytest.raises(GuestTimeout) as raised:
        run(main)
    assert str(raised.value) == f"[one] sleep 5: {GUEST_SAID}"
    assert vm.events == [{"error": f"timed out in the guest: {GUEST_SAID}"}]


def test_silence_past_the_deadline_is_not_a_guest_timeout() -> None:
    vm = Asked()

    async def main() -> None:
        await vm._ask("sleep 5", anyio.sleep_forever(), timeout=0.01)

    with pytest.raises(MachineError) as raised:
        run(main)
    assert not isinstance(raised.value, GuestTimeout)
    assert str(raised.value) == "[one] guest stopped answering during: sleep 5\n    | last console line"
    assert vm.events == [{"error": "the guest stopped answering"}]
