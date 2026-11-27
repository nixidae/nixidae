"""What the host must give a run's guests, checked before any of them boots.

A guest's modules declare what it needs (`vivarium.needs`): a device such
as `/dev/kvm`, or a path that exists only when the host has a function,
such as the sysctl `br_netfilter` adds. The spec carries the needs the
host must meet. A kernel need is the host's only under the container
backend, where the guest runs on the host's kernel. Each backend adds
what it checks itself (`Backend.missing`). The session asks `preflight`
before it starts the run, and refuses the run with every missing item.

A need checks the function, not a module name: a module built into the
kernel has no `/sys/module/<name>` when it takes no parameters.
"""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .machine_spec import MachineSpec


class NeedKind(StrEnum):
    DEVICE = "device"
    """A device node the guest's process opens for reading and writing."""
    PATH = "path"
    """A path that exists when the host has the function."""


class NeedJson(TypedDict):
    name: str
    kind: str
    path: str
    why: str
    remedy: str


@dataclass(frozen=True, slots=True)
class Need:
    """One entry of `vivarium.needs` that the host must meet."""

    name: str
    kind: NeedKind
    path: str
    why: str
    remedy: str

    @classmethod
    def from_json(cls, data: NeedJson) -> Need:
        return cls(data["name"], NeedKind(data["kind"]), data["path"], data["why"], data["remedy"])


@dataclass(frozen=True)
class Missing:
    """One thing the host lacks, and what gives it."""

    what: str
    why: str
    remedy: str

    def __str__(self) -> str:
        return f"{self.what}: {self.why}\n    fix: {self.remedy}"


def unmet(need: Need, root: Path = Path("/")) -> str | None:
    """Why the host at *root* does not meet *need*, or None when it does."""
    path = root / need.path.lstrip("/")
    if not path.exists():
        return f"{need.path} does not exist"
    if need.kind is NeedKind.DEVICE and not os.access(path, os.R_OK | os.W_OK):
        return f"{need.path} is not readable and writable by uid {os.getuid()}"
    return None


def missing(guests: Iterable[MachineSpec], root: Path = Path("/")) -> list[Missing]:
    """The declared needs of *guests* the host lacks: one item for each need, naming every guest that has it."""
    wanted: dict[Need, list[str]] = defaultdict(list)
    for guest in guests:
        for need in guest.needs:
            wanted[need].append(guest.name)
    found: list[Missing] = []
    for need, names in wanted.items():
        reason = unmet(need, root)
        if reason is not None:
            found.append(Missing(f"{need.name} ({reason})", f"{need.why} (guests: {', '.join(names)})", need.remedy))
    return found


class Checks(Protocol):
    """What `preflight` asks of a backend (`Backend.missing`)."""

    async def missing(self, specs: Sequence[MachineSpec]) -> list[Missing]: ...


async def preflight(guests: Sequence[MachineSpec], backend: Callable[[str], Checks]) -> list[Missing]:
    """Everything the host lacks for *guests*: their declared needs, then
    what each backend (*backend* of its name) checks itself."""
    found = missing(guests)
    by_backend: dict[str, list[MachineSpec]] = defaultdict(list)
    for guest in guests:
        by_backend[guest.backend].append(guest)
    for name, specs in sorted(by_backend.items()):
        found += await backend(name).missing(specs)
    return found
