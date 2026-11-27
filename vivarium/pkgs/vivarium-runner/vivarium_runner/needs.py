"""What the host must give a run's guests, checked before any of them boots.

A guest's modules declare what it needs (`vivarium.needs`): a device such
as `/dev/kvm`, a path that exists only when the host has a function, such
as the sysctl `br_netfilter` adds, or a probe: a command that uses the
function. The spec carries the needs the host must meet. A kernel need is
the host's only under the container backend, where the guest runs on the
host's kernel. Each backend adds what it checks itself (`Backend.missing`).
The session asks `preflight` before it starts the run, and refuses the run
with every missing item.

A need checks the function, not a module name: a module built into the
kernel has no `/sys/module/<name>` when it takes no parameters, and a
module the kernel loads on use may not be in its tree at all. A probe
runs where a container guest runs, in a user, network and mount
namespace of its own, so it loads what a guest would load (measured:
`nlmon` and `sch_tbf` load from such a namespace).
"""

from __future__ import annotations

import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NotRequired, Protocol, TypedDict

import anyio

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from .machine_spec import MachineSpec


class NeedKind(StrEnum):
    DEVICE = "device"
    """A device node the guest's process opens for reading and writing."""
    PATH = "path"
    """A path that exists when the host has the function."""
    PROBE = "probe"
    """A command that uses the function, in namespaces of its own; exit 0 is met."""


class NeedJson(TypedDict):
    name: str
    kind: str
    why: str
    remedy: str
    path: NotRequired[str]
    command: NotRequired[list[str]]


@dataclass(frozen=True, slots=True)
class Need:
    """One entry of `vivarium.needs` that the host must meet."""

    name: str
    kind: NeedKind
    why: str
    remedy: str
    path: str = ""
    """What a device or path need looks at."""
    command: tuple[str, ...] = ()
    """What a probe runs."""

    @classmethod
    def from_json(cls, data: NeedJson) -> Need:
        return cls(
            data["name"],
            NeedKind(data["kind"]),
            data["why"],
            data["remedy"],
            data.get("path", ""),
            tuple(data.get("command", ())),
        )


@dataclass(frozen=True)
class Missing:
    """One thing the host lacks, and what gives it."""

    what: str
    why: str
    remedy: str

    def __str__(self) -> str:
        return f"{self.what}: {self.why}\n    fix: {self.remedy}"


NAMESPACED = """
import os, sys
uid, gid = os.getuid(), os.getgid()
os.unshare(os.CLONE_NEWUSER | os.CLONE_NEWNET | os.CLONE_NEWNS)
for name, text in (("setgroups", "deny"), ("uid_map", f"0 {uid} 1"), ("gid_map", f"0 {gid} 1")):
    with open(f"/proc/self/{name}", "w") as file:
        file.write(text)
os.execv(sys.argv[1], sys.argv[1:])
"""
"""Runs argv as root of a user namespace with a network and a mount namespace
of its own: where a container guest's own programs run."""


def unmet(need: Need, root: Path = Path("/")) -> str | None:
    """Why the host at *root* does not meet a device or path *need*, or None when it does."""
    path = root / need.path.lstrip("/")
    if not path.exists():
        return f"{need.path} does not exist"
    if need.kind is NeedKind.DEVICE and not os.access(path, os.R_OK | os.W_OK):
        return f"{need.path} is not readable and writable by uid {os.getuid()}"
    return None


async def probe(need: Need) -> str | None:
    """Why *need*'s command failed, or None when it exited 0."""
    done = await anyio.run_process([sys.executable, "-I", "-c", NAMESPACED, *need.command], check=False)
    if done.returncode == 0:
        return None
    said = (done.stderr or done.stdout).decode(errors="replace").strip().splitlines()
    return f"{' '.join(need.command)} exited {done.returncode}" + (f": {said[-1]}" if said else "")


async def missing(guests: Iterable[MachineSpec], root: Path = Path("/")) -> list[Missing]:
    """The declared needs of *guests* the host lacks: one item for each need, naming every guest that has it."""
    wanted: dict[Need, list[str]] = defaultdict(list)
    for guest in guests:
        for need in guest.needs:
            wanted[need].append(guest.name)
    found: list[Missing] = []
    for need, names in wanted.items():
        reason = await probe(need) if need.kind is NeedKind.PROBE else unmet(need, root)
        if reason is not None:
            found.append(Missing(f"{need.name} ({reason})", f"{need.why} (guests: {', '.join(names)})", need.remedy))
    return found


class Checks(Protocol):
    """What `preflight` asks of a backend (`Backend.missing`)."""

    async def missing(self, specs: Sequence[MachineSpec]) -> list[Missing]: ...


async def preflight(guests: Sequence[MachineSpec], backend: Callable[[str], Checks]) -> list[Missing]:
    """Everything the host lacks for *guests*: their declared needs, then
    what each backend (*backend* of its name) checks itself."""
    found = await missing(guests)
    by_backend: dict[str, list[MachineSpec]] = defaultdict(list)
    for guest in guests:
        by_backend[guest.backend].append(guest)
    for name, specs in sorted(by_backend.items()):
        found += await backend(name).missing(specs)
    return found
