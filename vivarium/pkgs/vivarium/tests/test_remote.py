from __future__ import annotations

import inspect
from typing import Any

import anyio
import pytest
from vivarium.remote import CALLS, READABLE, MachinesService, RemoteMachine
from vivarium_runner.control import CommandResult
from vivarium_runner.machine import Machine, MachineError


class FakeMachine:
    uplink_ip = "10.0.2.15"

    async def succeed(
        self, command: str, timeout: float | None = None, env: dict[str, str] | None = None
    ) -> CommandResult:
        return CommandResult(0, f"ran {command}", "a warning")

    async def shutdown(self) -> None:
        raise AssertionError("the socket must not reach this")


class FakeSession:
    def __init__(self) -> None:
        self.vms_up = {"a": FakeMachine()}


class Loopback:
    """A connection whose calls reach a `MachinesService` in this process."""

    def __init__(self, service: MachinesService) -> None:
        self.service = service

    async def call(self, name: str, args: tuple[Any, ...], kwargs: dict[str, Any]) -> Any:  # noqa: ANN401 -- what the service returns
        result = getattr(self.service, f"exposed_{name}")(*args, **kwargs)
        return await result if inspect.isawaitable(result) else result


def proxy() -> RemoteMachine:
    return RemoteMachine(Loopback(MachinesService(FakeSession())), "a")  # type: ignore[arg-type]  # a fake of the connection


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.parametrize("name", sorted(CALLS))
def test_each_call_is_a_method_of_machine(name: str) -> None:
    """The type checker holds the signatures; this holds the names."""
    assert inspect.isfunction(getattr(Machine, name))
    assert inspect.signature(getattr(RemoteMachine, name)) == inspect.signature(getattr(Machine, name))


def test_every_readable_name_is_the_machines() -> None:
    assert all(hasattr(Machine, name) for name in READABLE)
    assert not READABLE & CALLS


@pytest.mark.anyio
async def test_a_call_crosses() -> None:
    assert await proxy().succeed("hostname") == CommandResult(0, "ran hostname", "a warning")


@pytest.mark.anyio
async def test_a_command_result_crosses_as_plain_data() -> None:
    """arpyc carries tuples and brine's own types, never a dataclass."""
    service = MachinesService(FakeSession())  # type: ignore[arg-type]  # a fake of the session
    assert await service.exposed_call("a", "succeed", ("hostname",), {}) == (0, "ran hostname", "a warning")


@pytest.mark.anyio
async def test_a_property_is_read_with_get() -> None:
    vm = proxy()
    assert await vm.get("uplink_ip") == "10.0.2.15"
    with pytest.raises(AttributeError, match=r"await vm\.get\('uplink_ip'\)"):
        _ = vm.uplink_ip  # type: ignore[attr-defined]  # the mistake under test


def test_a_method_off_the_list_is_named() -> None:
    with pytest.raises(AttributeError, match=r"Machine\.shutdown does not cross"):
        _ = proxy().shutdown  # type: ignore[attr-defined]  # the mistake under test


@pytest.mark.anyio
async def test_the_socket_serves_only_the_list() -> None:
    service = MachinesService(FakeSession())  # type: ignore[arg-type]  # a fake of the session
    with pytest.raises(AttributeError, match="not served"):
        await service.exposed_call("a", "shutdown", (), {})
    with pytest.raises(AttributeError, match="not readable"):
        service.exposed_get("a", "succeed")


@pytest.mark.anyio
async def test_until_runs_its_check_in_the_test() -> None:
    seen: list[int] = []

    async def check() -> int | None:
        seen.append(len(seen))
        return 7 if len(seen) == 3 else None

    assert await proxy().until("three", check, timeout=5, poll=0) == 7
    assert seen == [0, 1, 2]


@pytest.mark.anyio
async def test_until_raises_with_its_evidence() -> None:
    async def never_there() -> None:
        return None

    async def evidence() -> str:
        return "still down"

    with anyio.fail_after(5), pytest.raises(MachineError, match="not reached within 0s:\nstill down"):
        await proxy().until("up", never_there, timeout=0.05, poll=0.01, evidence=evidence)


@pytest.mark.anyio
async def test_never_raises_when_it_happens() -> None:
    async def happened() -> str:
        return "boom"

    with pytest.raises(MachineError, match="happened after"):
        await proxy().never("a crash", happened, timeout=5, poll=0)
