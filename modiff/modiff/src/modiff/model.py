"""What crosses from the engine thread to the UI: plain data, no `Value`."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Final

type Segment = str | int
type Path = tuple[Segment, ...]
type OptionPath = tuple[str, ...]

_BARE: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_'-]*")
EDIT_FILE: Final = "modiff edit: "
# The module system's own attribute: `_module.args`, `_module.check`.
MODULE_ATTR: Final = "_module"


class Kind(StrEnum):
    CHANGED = "changed"
    ADDED = "added"
    REMOVED = "removed"
    FAILED = "failed"
    # A changed set or list the walk compared at another path, `via`.
    SAME = "same"


@dataclass(frozen=True, slots=True)
class Change:
    """One difference under `config`. `old` and `new` are Nix-like text,
    except when `text` is set: then both are the raw strings, for a line
    diff. `via` is the other path a row refers to: where the walk met a
    failure that the message blames elsewhere, or where a SAME row's
    changes show."""

    kind: Kind
    path: Path
    old: str = ""
    new: str = ""
    text: bool = False
    via: Path = ()


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
    # The two above, paired.
    drvs: tuple[DrvChange, ...] = ()
    # Output path -> `.drv` path, both sides: output paths never collide.
    derivers: dict[str, str] = field(default_factory=dict)
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
    # An added or removed path's inputs that are added or removed with it
    # and that no other such path above them needs: removing nginx removes
    # its tarballs and patches.
    only: tuple[str, ...] = ()

    @property
    def path(self) -> str:
        return self.new or self.old or ""

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


DRV_SUFFIX: Final = ".drv"


def is_drv_file(path: str) -> bool:
    return path.endswith(DRV_SUFFIX)


def drv_file(path: str, derivers: dict[str, str]) -> str | None:
    """The `.drv` of a listed derivation, keyed by its output or its `.drv`."""
    return path if is_drv_file(path) else derivers.get(path)


def store_name(path: str) -> str:
    """`/nix/store/<hash>-openssh-9.8p1` -> `openssh-9.8p1`. A `.drv`
    is named as the derivation, so its outputs and it pair."""
    name = path.rsplit("/", 1)[-1].split("-", 1)[-1]
    return name.removesuffix(DRV_SUFFIX)


_VERSION_START: Final = re.compile(r"-(?![A-Za-z])")


def package_name(name: str) -> str:
    """Nix's `parseDrvName`: the version starts at the first dash that is
    not followed by a letter."""
    found = _VERSION_START.search(name)
    return name if found is None else name[: found.start()]


def pair_drvs(
    added: tuple[str, ...], removed: tuple[str, ...], likeness: Callable[[str, str], int] | None = None
) -> tuple[DrvChange, ...]:
    """Pair each new path with the old one of the same name, then of the
    same package name: `openssh-9.8p1` replaces `openssh-9.7p1`. Several
    paths of one name (two `unit-dbus-broker.service`) pair most alike
    first, by `likeness(old, new)`."""
    pairs: list[DrvChange] = []
    new_left = list(added)
    old_left = list(removed)
    for key in (store_name, lambda p: package_name(store_name(p))):
        olds_by: dict[str, list[str]] = {}
        news_by: dict[str, list[str]] = {}
        for path in old_left:
            olds_by.setdefault(key(path), []).append(path)
        for path in new_left:
            news_by.setdefault(key(path), []).append(path)
        for name, news in news_by.items():
            olds = olds_by.get(name, [])
            candidates = [(o, n) for n in news for o in olds]
            if likeness is not None and len(candidates) > 1:
                # Stable: equal likeness keeps the order of the paths.
                candidates.sort(key=lambda c: -likeness(*c))
            for old, new in candidates:
                if old in old_left and new in new_left:
                    old_left.remove(old)
                    new_left.remove(new)
                    pairs.append(DrvChange(old, new))
    pairs += [DrvChange(None, p) for p in new_left]
    pairs += [DrvChange(p, None) for p in old_left]
    return tuple(sorted(pairs, key=lambda c: (c.name, c.kind)))


FOLDED: Final = frozenset({Rebuild.ADDED, Rebuild.REMOVED})


def fold_inputs(changes: tuple[DrvChange, ...], inputs: Callable[[str], Iterable[str]]) -> tuple[DrvChange, ...]:
    """Each added or removed path that another one of its kind needs, as
    `only` of the top one that reaches it first. `inputs(path)` names the
    paths a path needs directly. A rebuilt pair stays as it is: its inputs
    rebuilt too, each for a reason the user may want to see."""
    out: list[DrvChange] = []
    for kind in sorted(FOLDED):
        single = {c.path: c for c in changes if c.kind is kind}
        below = {p: [i for i in inputs(p) if i in single and i != p] for p in single}
        needed = {i for found in below.values() for i in found}
        taken: set[str] = set()
        for top in sorted(p for p in single if p not in needed):
            # Breadth first: what the top needs directly comes first.
            folded: list[str] = []
            queue = deque(sorted(below[top], key=store_name))
            while queue:
                p = queue.popleft()
                if p in taken:
                    continue
                taken.add(p)
                folded.append(p)
                queue.extend(sorted(below[p], key=store_name))
            out.append(replace(single[top], only=tuple(folded)))
    out += [c for c in changes if c.kind not in FOLDED]
    return tuple(sorted(out, key=lambda c: (c.name, c.kind)))


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


def changed_paths(
    old: set[str], new: set[str], likeness: Callable[[str, str], int] | None = None
) -> tuple[DrvChange, ...]:
    return pair_drvs(tuple(sorted(new - old)), tuple(sorted(old - new)), likeness)


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


MODULE_HEAD: Final = """\
# The edits modiff had on, as a module.
{ lib, config, ... }:
# modiff puts the module arguments, such as `pkgs`, in scope this way.
with config._module.args;
"""


def edits_module(edits: list[Edit]) -> str:
    """The enabled edits as one module, with the meaning modiff gave them:
    each edit is its own definition. Under `config`, a path never meets
    a module key such as `imports`."""
    on = [e for e in edits if e.enabled]

    def define(edit: Edit) -> str:
        # As typed: indenting a `"…"` string that spans lines changes it.
        # A `#` on the last line may start a comment, which would take the `;`.
        end = "\n;" if "#" in edit.expr.rsplit("\n", 1)[-1] else ";"
        return f"{format_path(edit.path)} = {edit.expr}{end}"

    # `a = lib.mkForce { … }; a.b = 1;` is a Nix error; two modules merge.
    nested = any(a.path != b.path and overlaps(a.path, b.path) for a in on for b in on)
    if not nested:
        body = "".join(f"    {define(e)}\n" for e in on)
        return f"{MODULE_HEAD}{{\n  config = {{\n{body}  }};\n}}\n"
    items = "".join(f"    {{ {define(e)} }}\n" for e in on)
    return f"{MODULE_HEAD}{{\n  config = lib.mkMerge [\n{items}  ];\n}}\n"


def overlaps(a: Path, b: Path) -> bool:
    """One path is the other or lies under it."""
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def nix_string(s: str) -> str:
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("${", "\\${")
    return f'"{escaped}"'


_UNESCAPE: Final = {"n": "\n", "r": "\r", "t": "\t"}


def expand_strings(printed: str) -> str:
    """Printed Nix with each string that holds a newline as an indented
    `''` string, a line per line, so a line diff shows the lines that
    changed. For reading: a string whose lines all share leading spaces
    reads with them stripped, as Nix strips them."""
    out: list[str] = []
    # The indentation of the output line being written.
    indent = ""
    at_line_start = True
    i = 0
    while i < len(printed):
        char = printed[i]
        if char != '"':
            out.append(char)
            if char == "\n":
                indent, at_line_start = "", True
            elif char == " " and at_line_start:
                indent += " "
            else:
                at_line_start = False
            i += 1
            continue
        at_line_start = False
        start = i
        i += 1
        decoded: list[str] = []
        while i < len(printed) and printed[i] != '"':
            if printed[i] == "\\" and i + 1 < len(printed):
                decoded.append(_UNESCAPE.get(printed[i + 1], printed[i + 1]))
                i += 2
            else:
                decoded.append(printed[i])
                i += 1
        i += 1
        text = "".join(decoded)
        if "\n" not in text:
            out.append(printed[start:i])
            continue
        body = text.replace("''", "'''").replace("${", "''${").replace("\r", "''\\r")
        lines = body.split("\n")
        shown = [f"{indent}  {line}" if line else "" for line in lines[:-1]]
        shown.append(f"{indent}  {lines[-1]}''" if lines[-1] else f"{indent}''")
        out.append("''\n" + "\n".join(shown))
    return "".join(out)


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


# `lib/modules.nix`, `lib/options.nix`, `lib/types.nix`: "The option `a.b'
# has conflicting definition values", "A definition for option `a.b' is not
# of type". Some messages close the quote with a backtick. Anchored: a
# NixOS "Failed assertions:" message may quote an option it is not about.
_BLAMED: Final = re.compile(r"(?:The option|A definition for option) `([^`'\n]+)[`']")


def blamed_path(message: str) -> OptionPath | None:
    """The option path a module system error is about, if any."""
    found = _BLAMED.match(message.strip())
    return None if found is None else parse_option_path(found.group(1))


def edited_path(failed: Iterable[Path], edited: Iterable[Path]) -> Path | None:
    """The first failed path that is a path the edits set, else the first
    under one: a `throw` in an edit names no option, and the walk meets it
    first wherever a reader of the option sorts. That reader may be edited
    too, so the failed option itself comes first."""
    paths, prefixes = list(failed), set(edited)
    exact = next((p for p in paths if p in prefixes), None)
    return exact or next((p for p in paths if any(p[: len(e)] == e for e in prefixes)), None)
