"""Run NixOS systems as guests and drive them from Python.

The mechanism under `vivarium`: guests on four backends (User-Mode
Linux, QEMU, libkrun and crun), their networks and forwards, and the agent a test
talks to. A phase script is a coroutine over :class:`Machines`.
"""

from __future__ import annotations

from vivarium_agent.channel import ProcessInfo, SystemState, UnitInfo

from .display import Button
from .forward import ForwardError
from .machine import GuestTimeout, Machine, MachineError
from .machine_spec import Interface, LaunchSpec, MachineSpec, Systems
from .machines import Issuer, Machines, Trust
from .net import Lan, SegmentError

__all__ = [
    "Button",
    "ForwardError",
    "GuestTimeout",
    "Interface",
    "Issuer",
    "Lan",
    "LaunchSpec",
    "Machine",
    "MachineError",
    "MachineSpec",
    "Machines",
    "ProcessInfo",
    "SegmentError",
    "SystemState",
    "Systems",
    "Trust",
    "UnitInfo",
]
