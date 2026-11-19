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


@dataclass(frozen=True, slots=True)
class Group:
    """The changes under one option. `option` is empty when no option
    declares the path."""

    option: OptionPath
    changes: tuple[Change, ...]


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
