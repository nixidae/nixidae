"""What the host and the guest agent say to each other: the ready line and
the shapes of the agent's replies.

Apart from `agent`, so the host never imports the agent's module and what
only the guest needs (dinit-client). A caller that resolves the runner in
a Python set of its own, as pynixd does, has no dinit-client.
"""

from __future__ import annotations

from typing import Protocol, TypedDict

AGENT_READY = "vivarium-agent: ready"
"""Printed on the guest console once the agent is reading its device; the
host waits for this line to know a guest has finished booting, and
connects the moment it sees it.  Nothing may be printed before the line
is set up, or the first request races the setup and is lost."""


class UnitInfo(TypedDict):
    """One line of ``systemctl list-units``."""

    name: str
    load: str
    active: str
    sub: str
    description: str


class SystemState(TypedDict):
    """How a guest's boot ended: ``systemctl is-system-running``'s word
    (``running``, ``degraded``, ...) and the units or services that failed.
    ``starting`` is a boot that did not end within the timeout."""

    state: str
    failed: tuple[str, ...]
    """A tuple: arpyc carries a list as one."""


class ProcessInfo(TypedDict):
    """One entry of ``/proc``: see `agent.Agent.exposed_processes`."""

    pid: int
    ppid: int
    name: str
    cmdline: str


class AgentRpc(Protocol):
    """The agent's methods as the host calls them: `agent.Agent`'s
    ``exposed_*``, by name. `tests/test_channel.py` holds the two together."""

    async def run(self, command: str, timeout: float, env: dict[str, str] | None) -> tuple[int, str, str]: ...
    async def system_state(self, timeout: float) -> SystemState: ...
    async def poweroff(self) -> None: ...
    async def unit_state(self, unit: str) -> str: ...
    async def unit_info(self, unit: str) -> dict[str, str]: ...
    async def list_units(self, pattern: str) -> list[UnitInfo]: ...
    async def listening(self) -> list[int]: ...
    async def processes(self) -> list[ProcessInfo]: ...
    async def journal(self, unit: str | None, lines: int) -> str: ...
    async def relay(self, path: str, method: str, args: tuple[object, ...], kwargs: dict[str, object]) -> object: ...
