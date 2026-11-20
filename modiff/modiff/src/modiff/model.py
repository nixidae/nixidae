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
EDIT_FILE: Final = "modiff edit: "


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


class Rebuild(StrEnum):
    REBUILT = "rebuilt"
    VERSION = "version"
    ADDED = "added"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class DrvChange:
    """One output path the edits changed, paired with the one it replaces."""

    old: str | None
    new: str | None

    @property
    def kind(self) -> Rebuild:
        if self.old is None:
            return Rebuild.ADDED
        if self.new is None:
            return Rebuild.REMOVED
        return Rebuild.REBUILT if store_name(self.old) == store_name(self.new) else Rebuild.VERSION

    @property
    def name(self) -> str:
        return store_name(self.new or self.old or "")


def store_name(path: str) -> str:
    """`/nix/store/<hash>-openssh-9.8p1` -> `openssh-9.8p1`."""
    return path.rsplit("/", 1)[-1].split("-", 1)[-1]


_VERSION_START: Final = re.compile(r"-(?![A-Za-z])")


def package_name(name: str) -> str:
    """Nix's `parseDrvName`: the version starts at the first dash that is
    not followed by a letter."""
    found = _VERSION_START.search(name)
    return name if found is None else name[: found.start()]


def pair_drvs(added: tuple[str, ...], removed: tuple[str, ...]) -> tuple[DrvChange, ...]:
    """Pair each new path with the old one of the same name, then of the
    same package name: `openssh-9.8p1` replaces `openssh-9.7p1`."""
    pairs: list[DrvChange] = []
    new_left = list(added)
    old_left = list(removed)
    for key in (store_name, lambda p: package_name(store_name(p))):
        by_key: dict[str, list[str]] = {}
        for path in old_left:
            by_key.setdefault(key(path), []).append(path)
        unpaired: list[str] = []
        for path in new_left:
            olds = by_key.get(key(path))
            if olds:
                old = olds.pop(0)
                old_left.remove(old)
                pairs.append(DrvChange(old, path))
            else:
                unpaired.append(path)
        new_left = unpaired
    pairs += [DrvChange(None, p) for p in new_left]
    pairs += [DrvChange(p, None) for p in old_left]
    return tuple(sorted(pairs, key=lambda c: (c.name, c.kind)))


@dataclass(frozen=True, slots=True)
class AttrChange:
    """One attribute of a `.drv` that differs: `builder`, `args`,
    `system`, or a variable of its environment."""

    name: str
    old: str | None
    new: str | None


@dataclass(frozen=True, slots=True)
class DrvDiff:
    """Why a derivation rebuilds: what differs between its two `.drv`s."""

    old: str = ""
    new: str = ""
    inputs: tuple[DrvChange, ...] = ()
    sources: tuple[DrvChange, ...] = ()
    attrs: tuple[AttrChange, ...] = ()
    error: str | None = None


def changed_paths(old: set[str], new: set[str]) -> tuple[DrvChange, ...]:
    return pair_drvs(tuple(sorted(new - old)), tuple(sorted(old - new)))


def changed_attrs(old: dict[str, str], new: dict[str, str]) -> tuple[AttrChange, ...]:
    return tuple(
        AttrChange(name, old.get(name), new.get(name))
        for name in sorted(old.keys() | new.keys())
        if old.get(name) != new.get(name)
    )


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
    # False when the base never evaluated the option: then `defs_old` is
    # empty, because listing definitions evaluates them.
    base_read: bool = True
    # `highestPrio`: the priority every surviving definition shares.
    prio_old: int | None = None
    prio_new: int | None = None
    errors: tuple[str, ...] = field(default=())


# `lib/modules.nix`: what a priority is called where a module sets it.
PRIORITY_NAMES: Final = {
    10: "lib.mkVMOverride",
    50: "lib.mkForce",
    60: "lib.mkImageMediaOverride",
    100: "a plain definition",
    1000: "lib.mkDefault",
    1500: "the option's default",
}


def priority_text(prio: int) -> str:
    name = PRIORITY_NAMES.get(prio)
    return f"{prio}, {name}" if name else f"{prio}, lib.mkOverride {prio}"


def edit_file(path: OptionPath) -> str:
    """The `_file` of an edit's module: how its definitions are named."""
    return EDIT_FILE + format_path(path)


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
