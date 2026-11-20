"""What crosses from the engine thread to the UI: plain data, no `Value`."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final

type Segment = str | int
type Path = tuple[Segment, ...]
type OptionPath = tuple[str, ...]

_BARE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]*")


class Kind(StrEnum):
    CHANGED = "changed"
    ADDED = "added"
    REMOVED = "removed"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class Change:
    """One difference under `config`. `old` and `new` are Nix-like text,
    except when `text` is set: then both are the raw strings, for a line
    diff."""

    kind: Kind
    path: Path
    old: str = ""
    new: str = ""
    text: bool = False


class Quiet(StrEnum):
    """Why an edit changed nothing that was evaluated."""

    DROPPED = "dropped"
    # The cascade is not empty: the edit may act through another path, as
    # a renamed option does, so this row claims nothing about it.
    ELSEWHERE = "elsewhere"
    UNREAD = "unread"
    SAME = "same"
    # Equal as far as the base evaluated it; the rest was never read.
    PARTIAL = "partial"


@dataclass(frozen=True, slots=True)
class Group:
    """The changes under one option. `option` is empty when no option
    declares the path. A group with `quiet` set stands for an edit that
    changed nothing, and says why."""

    option: OptionPath
    changes: tuple[Change, ...]
    quiet: Quiet | None = None


@dataclass(frozen=True, slots=True)
class Edit:
    path: OptionPath
    expr: str
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class Loaded:
    seconds: float
    forced: int
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Cascade:
    groups: tuple[Group, ...] = ()
    seconds: float = 0.0
    visited: int = 0
    drvs_added: tuple[str, ...] = ()
    drvs_removed: tuple[str, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Definition:
    file: str
    value: str


@dataclass(frozen=True, slots=True)
class OptionView:
    path: OptionPath
    type_description: str = ""
    description: str = ""
    declarations: tuple[str, ...] = ()
    default: str = ""
    old: str = ""
    new: str = ""
    defs_old: tuple[Definition, ...] = ()
    defs_new: tuple[Definition, ...] = ()
    errors: tuple[str, ...] = field(default=())


def overlaps(a: Path, b: Path) -> bool:
    """One path is the other or lies under it."""
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def nix_string(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    return f'"{escaped}"'


def format_path(path: Path) -> str:
    out = ""
    for segment in path:
        if isinstance(segment, int):
            out += f"[{segment}]"
        else:
            name = segment if _BARE.fullmatch(segment) else nix_string(segment)
            out += f".{name}" if out else name
    return out


def parse_option_path(text: str) -> OptionPath:
    """`a.b."c.d"` -> ("a", "b", "c.d")."""
    parts: list[str] = []
    i = 0
    while i < len(text):
        if text[i] == '"':
            end = i + 1
            while end < len(text) and text[end] != '"':
                end += 2 if text[end] == "\\" else 1
            parts.append(text[i + 1 : end].replace('\\"', '"').replace("\\\\", "\\"))
            i = end + 2
        else:
            end = text.find(".", i)
            end = len(text) if end < 0 else end
            parts.append(text[i:end])
            i = end + 1
    return tuple(p for p in parts if p)
