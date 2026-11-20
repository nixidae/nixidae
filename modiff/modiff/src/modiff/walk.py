"""Compare what an evaluation forced against the same paths in another one.

Runs on the engine thread: every `Value` here belongs to it.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Final

from huggorm import EvalState, Value
from huggorm.errors import NixError

from .model import Change, Kind, Path

SKIP: Final = frozenset({"_module"})
# A derivation is keyed by the first of these its evaluation forced.
DRV_KEYS: Final = ("outPath", "drvPath", "name")


def error_text(err: NixError) -> str:
    """The message after Nix's last `error:`, without the trace above it."""
    text = str(err)
    at = text.rfind("error:")
    body = text[at + len("error:") :] if at >= 0 else text
    return body.strip().split("\n\n", 1)[0]


def is_derivation(v: Value) -> bool:
    """An attrset with `outPath`, as `toString` reads it; `has` does not
    force. A derivation leads into nixpkgs through passthru and drvAttrs:
    3.9 M unforced children on a minimal NixOS config, none of them module
    config. Testing `type == "derivation"` misses the ones whose `type`
    nothing forced."""
    return v.type_name() == "attrs" and v.has("outPath")


def forced_attr(v: Value, name: str) -> Value | None:
    if not v.has(name):
        return None
    child = v.get(name)
    return None if child.type_name() == "thunk" else child


def drv_attr(v: Value) -> str | None:
    for name in DRV_KEYS:
        if forced_attr(v, name) is not None:
            return name
    return None


def scalar(v: Value) -> str:
    match v.type_name():
        case "int":
            return str(v.integer())
        case "float":
            return str(v.floating())
        case "bool":
            return "true" if v.boolean() else "false"
        case "string":
            return str(v.string_value())
        case "null":
            return "null"
        case kind:
            return f"«{kind}»"


@dataclass(slots=True)
class Walker:
    state: EvalState
    changes: list[Change] = field(default_factory=list)
    failures: dict[str, list[Path]] = field(default_factory=dict)
    visited: int = 0

    def force(self, v: Value, path: Path) -> bool:
        """Force a value of the new evaluation. A failure is a result: it
        lands in `failures`, keyed by message, because one broken option
        fails everything downstream of it."""
        try:
            self.state.force(v)
        except NixError as err:
            self.failures.setdefault(error_text(err), []).append(path)
            return False
        return True

    def result(self) -> list[Change]:
        failed = [
            Change(Kind.FAILED, paths[0], f"{len(paths)} paths" if len(paths) > 1 else "", message)
            for message, paths in self.failures.items()
        ]
        return failed + self.changes

    def drv_key(self, v: Value, name: str | None, path: Path) -> str | None:
        if name is None or not v.has(name):
            return "«derivation»"
        child = v.get(name)
        if not self.force(child, path):
            return None
        return f"«{name} {scalar(child)}»"

    def compare(self, old: Value, new: Value, path: Path, above: set[int]) -> None:
        """`above` holds the containers on the current path only: enough to
        stop a cycle. A global set hid changes, because Nix shares one value
        for every `true`, so `{ p = true; q = true; }` walked `q` as seen."""
        kind = old.type_name()
        if kind == "thunk":
            return
        if kind not in {"attrs", "list"}:
            self._compare(old, new, kind, path, above)
            return
        ident = old.identity()
        if ident in above:
            return
        above.add(ident)
        try:
            self._compare(old, new, kind, path, above)
        finally:
            above.discard(ident)

    def _compare(self, old: Value, new: Value, kind: str, path: Path, above: set[int]) -> None:
        self.visited += 1
        if not self.force(new, path):
            return
        new_kind = new.type_name()
        if kind != new_kind:
            self.changes.append(Change(Kind.CHANGED, path, f"«{kind}»", f"«{new_kind}»"))
            return
        if kind == "attrs" and is_derivation(old):
            name = drv_attr(old)
            a, b = self.drv_key(old, name, path), self.drv_key(new, name, path)
            if a is not None and b is not None and a != b:
                self.changes.append(Change(Kind.CHANGED, path, a, b))
        elif kind == "attrs":
            self._attrs(old, new, path, above)
        elif kind == "list":
            self._list(old, new, path, above)
        else:
            a, b = scalar(old), scalar(new)
            if a != b:
                self.changes.append(Change(Kind.CHANGED, path, a, b, text=kind == "string"))

    def _attrs(self, old: Value, new: Value, path: Path, above: set[int]) -> None:
        old_names = set(old.names()) - SKIP
        new_names = set(new.names()) - SKIP
        for name in sorted(new_names - old_names):
            self.changes.append(Change(Kind.ADDED, (*path, name)))
        for name in sorted(old_names - new_names):
            self.changes.append(Change(Kind.REMOVED, (*path, name)))
        for name in sorted(old_names & new_names):
            self.compare(old.get(name), new.get(name), (*path, name), above)

    def align_key(self, v: Value, path: Path) -> str | None:
        """What a list element is matched on: the scalar, a derivation's
        name, or its kind. Forces it: an element the base forced is forced
        in the new evaluation anyway."""
        if not self.force(v, path):
            return None
        if is_derivation(v):
            return self.drv_key(v, "name", path)
        kind = v.type_name()
        return f"«{kind}»" if kind in {"attrs", "list"} else scalar(v)

    def _list(self, old: Value, new: Value, path: Path, above: set[int]) -> None:
        olds = [old.at(i) for i in range(old.length())]
        news = [new.at(i) for i in range(new.length())]
        a_keys = (
            None
            if any(v.type_name() == "thunk" for v in olds)
            else [self.align_key(v, (*path, i)) for i, v in enumerate(olds)]
        )
        b_keys = None if a_keys is None else [self.align_key(v, (*path, i)) for i, v in enumerate(news)]
        if a_keys is None or b_keys is None or None in a_keys or None in b_keys:
            # Unforced or failed elements cannot be aligned: compare by index.
            if len(olds) != len(news):
                self.changes.append(Change(Kind.CHANGED, (*path, "length"), str(len(olds)), str(len(news))))
            for i, (a, b) in enumerate(zip(olds, news, strict=False)):
                self.compare(a, b, (*path, i), above)
            return
        matcher = difflib.SequenceMatcher(a=a_keys, b=b_keys, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                    self.compare(olds[i], news[j], (*path, j), above)
                continue
            for i in range(i1, i2):
                self.changes.append(Change(Kind.REMOVED, (*path, i), a_keys[i] or ""))
            for j in range(j1, j2):
                self.changes.append(Change(Kind.ADDED, (*path, j), "", b_keys[j] or ""))


def count_forced(v: Value, above: set[int] | None = None) -> int:
    """Forced values under `v`, derivations counted as one."""
    above = set() if above is None else above
    kind = v.type_name()
    if kind == "thunk":
        return 0
    if kind not in {"attrs", "list"} or is_derivation(v):
        return 1
    ident = v.identity()
    if ident in above:
        return 0
    above.add(ident)
    try:
        if kind == "attrs":
            return 1 + sum(count_forced(v.value_at(i), above) for i in range(v.size()) if v.name_at(i) not in SKIP)
        return 1 + sum(count_forced(v.at(i), above) for i in range(v.length()))
    finally:
        above.discard(ident)


def show_lazy(v: Value) -> str:
    """`v` as far as it was evaluated, as Nix prints it: `«thunk»` marks
    what nothing needed. Forces nothing, so showing a value never widens
    what the next cascade compares. Nix prints a derivation as its path
    only when it may force (`print.cc`, `printDerivation`): modiff names a
    top-level one itself."""
    if is_derivation(v):
        for name in ("drvPath", "outPath", "name"):
            child = forced_attr(v, name)
            if child is not None and child.type_name() == "string":
                return f"«derivation {child.string_value()}»"
        return "«derivation»"
    return v.print_value(max_depth=6, max_attrs=200, max_list_items=200, max_string_length=4000, pretty_indent=2)


def show_forced(v: Value) -> str:
    """`v` evaluated, a derivation as its `.drv` path. For option metadata
    only: a configuration value it forces is one the next cascade compares."""
    try:
        return v.print_value(
            force=True,
            derivation_paths=True,
            max_depth=4,
            max_attrs=100,
            max_list_items=100,
            max_string_length=4000,
            pretty_indent=2,
        )
    except NixError as err:
        return f"«error: {error_text(err)}»"
