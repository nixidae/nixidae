"""What the host and the guest agent say to each other: the ready line and
the shapes of the agent's replies.

Apart from `agent`, so the host never imports the agent's module and what
only the guest needs (dinit-client). A caller that resolves the runner in
a Python set of its own, as pynixd does, has no dinit-client.
"""

from __future__ import annotations

from typing import TypedDict

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
    (``running``, ``degraded``, ...) and the units or services that failed."""

    state: str
    failed: tuple[str, ...]
    """A tuple: arpyc carries a list as one."""


class ProcessInfo(TypedDict):
    """One entry of ``/proc``: see `agent.Agent.exposed_processes`."""

    pid: int
    ppid: int
    name: str
    cmdline: str
