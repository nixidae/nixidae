"""Compare what an evaluation forced against the same paths in another one.

Runs on the engine thread: every `Value` here belongs to it.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Final

from huggorm import EvalState, Value
from huggorm.errors import NixError

from .model import MODULE_ATTR, Change, Kind, Path, nix_string

SKIP: Final = frozenset({MODULE_ATTR})
# A derivation is keyed by the first of these its evaluation forced.
DRV_KEYS: Final = ("outPath", "drvPath", "name")
INSTANTIATED_ORDER: Final = ("outPath", "drvPath")
INSTANTIATED: Final = frozenset(INSTANTIATED_ORDER)

# A list element's key: field name to its value as Nix prints it. `SELF` is
# the element itself, a scalar or a kind. A field a side lacks is unknown
# there, not different.
type Key = dict[str, str]
SELF: Final = ""
SCALARS: Final = frozenset({"int", "float", "bool", "string", "null", "path"})
# Not a value: nothing forced a thunk, and forcing a failed one threw. In
# the base a `tryEval` leaves one failed; comparing it forces the new side
# and reports the base's own failure as the edit's.
UNEVALUATED: Final = frozenset({"thunk", "failed"})
# Above this many element pairs, a stretch between anchors pairs by index:
# the match is quadratic in Python.
MATCH_CELLS: Final = 40_000
# Nix's print budget counts items across the whole value, not per list. At
# 200 the openssh edit's change to `assertions` from users-groups.nix
# printed the same on both sides. At 2000 the largest NixOS view
# (`environment.etc`, about 1 MB) renders in 0.07 s.
PRINT_ITEMS: Final = 2000


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
    return None if child.type_name() in UNEVALUATED else child


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
    # Derivations instantiated on both sides whose paths differ. Nix
    # records a derivation only when it coerces one to a string, and the
    # walk never does: without these, a top level like
    # `system.build.toplevel` is missing from the rebuilds.
    rebuilt: list[tuple[Value, Value]] = field(default_factory=list)

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
            Change(Kind.FAILED, paths[0], other_paths(len(paths) - 1), message)
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
        if kind in UNEVALUATED:
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
                if name in INSTANTIATED:
                    self.rebuilt.append((old, new))
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

    def element_key(self, v: Value, fields: frozenset[str] = frozenset()) -> Key:
        """What a list element is matched on. Forces the element, a
        derivation's name and the attributes in `fields`, nothing deeper,
        and records no failure: an inserted element may be one nothing
        evaluates."""
        if v.type_name() in UNEVALUATED and not quiet_force(self.state, v):
            return {SELF: "«error»"}
        kind = v.type_name()
        if is_derivation(v):
            name = v.get("name") if v.has("name") else None
            if name is None or not quiet_force(self.state, name):
                return {SELF: "«derivation»"}
            return {SELF: f"«name {scalar(name)}»"}
        if kind == "attrs":
            for field in fields:
                if v.has(field):
                    quiet_force(self.state, v.get(field))
            return {SELF: "«attrs»"} | known_fields(v)
        if kind == "list":
            return {SELF: "«list»"}
        return {SELF: scalar(v)}

    def _list(self, old: Value, new: Value, path: Path, above: set[int]) -> None:
        olds = [old.at(i) for i in range(old.length())]
        news = [new.at(i) for i in range(new.length())]
        a_keys = [{} if v.type_name() in UNEVALUATED else self.element_key(v) for v in olds]
        # A `listOf` element is a merge thunk of its own: often the base
        # forced the list and no element. Then no key anchors anything, and
        # forcing the new elements would print values where the base
        # prints «thunk».
        b_keys: list[Key] = (
            [self.element_key(v, common_fields(a_keys)) for v in news] if any(a_keys) else [{} for _ in news]
        )
        for i, j in align(a_keys, b_keys):
            if i is not None and j is not None:
                self.compare(olds[i], news[j], (*path, j), above)
            elif i is not None:
                self.changes.append(Change(Kind.REMOVED, (*path, i), describe(a_keys[i])))
            elif j is not None:
                # The base has no counterpart: its value is no fake change.
                key = b_keys[j] or self.element_key(news[j])
                self.changes.append(Change(Kind.ADDED, (*path, j), "", describe(key)))

    def force_like(self, old: Value, new: Value, above: set[int] | None = None) -> None:
        """Force `new` as far as `old` is forced, and record nothing. A walk
        that met a failure stops early, and its new side then prints
        «thunk» where the base prints a value: a change that is not one.
        Only for the new side, which every apply replaces."""
        above = set() if above is None else above
        kind = old.type_name()
        if kind in UNEVALUATED or not quiet_force(self.state, new) or new.type_name() != kind:
            return
        if kind not in {"attrs", "list"}:
            return
        if is_derivation(old):
            # One level: below are drvAttrs and passthru, into nixpkgs.
            for name in old.names():
                if old.get(name).type_name() not in UNEVALUATED and new.has(name):
                    quiet_force(self.state, new.get(name))
            return
        ident = old.identity()
        if ident in above:
            return
        above.add(ident)
        try:
            if kind == "attrs":
                for name in sorted((set(old.names()) & set(new.names())) - SKIP):
                    self.force_like(old.get(name), new.get(name), above)
                return
            olds = [old.at(i) for i in range(old.length())]
            news = [new.at(i) for i in range(new.length())]
            a_keys = [{} if v.type_name() in UNEVALUATED else self.element_key(v) for v in olds]
            if not any(a_keys):
                return
            b_keys = [self.element_key(v, common_fields(a_keys)) for v in news]
            for i, j in align(a_keys, b_keys):
                if i is not None and j is not None:
                    self.force_like(olds[i], news[j], above)
        finally:
            above.discard(ident)


def other_paths(count: int) -> str:
    """How many more paths failed with one message."""
    return "" if count == 0 else f"{count} other path{'' if count == 1 else 's'}"


def drv_string(state: EvalState, drv: Value, name: str) -> str | None:
    """`outPath` or `drvPath` of a derivation that was instantiated: one
    of them was forced. Both select from one `derivationStrict` result,
    so this evaluates nothing new. It still turns a thunk into a value,
    which `show_lazy` and the walk would read: use `instantiated` on the
    base."""
    if not drv.has(name):
        return None
    v = drv.get(name)
    if not quiet_force(state, v) or v.type_name() != "string":
        return None
    return str(v.string_value())


def instantiated(drv: Value) -> str | None:
    """The path the walk keyed a derivation on: its `outPath`, else its
    `.drv`. Forces nothing."""
    for name in INSTANTIATED_ORDER:
        child = forced_attr(drv, name)
        if child is not None and child.type_name() == "string":
            return str(child.string_value())
    return None


def quiet_force(state: EvalState, v: Value) -> bool:
    try:
        state.force(v)
    except NixError:
        return False
    return True


def literal(v: Value) -> str:
    return nix_string(str(v.string_value())) if v.type_name() == "string" else scalar(v)


def known_fields(v: Value) -> Key:
    """The scalar fields of `v` that are already values. Nix makes no thunk
    for a constant, so a literal field is known on both sides, evaluated or
    not."""
    out: Key = {}
    for i in range(v.size()):
        name = v.name_at(i)
        child = v.value_at(i)
        if name not in SKIP and child.type_name() in SCALARS:
            out[name] = literal(child)
    return out


def common_fields(keys: list[Key]) -> frozenset[str]:
    """The fields known in every old attrset element: the ones to force
    on the new side, where a submodule's fields are all thunks. Forcing
    there is safe, as every apply replaces the new configuration. A field
    known in only some elements may be a literal there and a thunk the
    base never forced elsewhere (NixOS `assertions` `message`): forcing
    it prints a value where the base prints «thunk»."""
    attrs = [set(key) - {SELF} for key in keys if key.get(SELF) == "«attrs»"]
    return frozenset(set.intersection(*attrs)) if attrs else frozenset()


def describe(key: Key) -> str:
    fields = [f"{name} = {value};" for name, value in key.items() if name != SELF]
    if not fields:
        return key.get(SELF, "«thunk»")
    shown = " ".join(f if len(f) <= 60 else f[:57] + "…;" for f in fields[:3])
    return f"{{ {shown}{' …' if len(fields) > 3 else ''} }}"


def compatible(old: Key, new: Key) -> bool:
    return all(new.get(name, value) == value for name, value in old.items())


def _anchors(a: list[Key], b: list[Key]) -> list[tuple[int, int]]:
    """Pairs whose string fields are equal and unique on both sides,
    longest run in order: patience diff's anchors. Strings only: a
    literal string is a value on both sides, while a computed `bool` the
    base forced is still a thunk on the new side."""

    def unique(keys: list[Key]) -> dict[frozenset[tuple[str, str]], int]:
        seen: dict[frozenset[tuple[str, str]], int] = {}
        repeated: set[frozenset[tuple[str, str]]] = set()
        for i, key in enumerate(keys):
            frozen = frozenset((k, v) for k, v in key.items() if k == SELF or v.startswith('"'))
            if frozen in seen:
                repeated.add(frozen)
            seen[frozen] = i
        return {k: i for k, i in seen.items() if k not in repeated and k}

    ua, ub = unique(a), unique(b)
    pairs = sorted((i, ub[k]) for k, i in ua.items() if k in ub)
    # Longest increasing run of `j`, by patience sorting.
    tails: list[int] = []
    tail_js: list[int] = []
    back: list[int] = [-1] * len(pairs)
    for n, (_, j) in enumerate(pairs):
        at = bisect.bisect_left(tail_js, j)
        if at > 0:
            back[n] = tails[at - 1]
        if at == len(tails):
            tails.append(n)
            tail_js.append(j)
        else:
            tails[at] = n
            tail_js[at] = j
    run: list[tuple[int, int]] = []
    n = tails[-1] if tails else -1
    while n >= 0:
        run.append(pairs[n])
        n = back[n]
    return run[::-1]


def _stretch(a: list[Key], b: list[Key], i0: int, i1: int, j0: int, j1: int) -> list[tuple[int | None, int | None]]:
    """Align `a[i0:i1]` with `b[j0:j1]`, where no anchor is."""
    head: list[tuple[int | None, int | None]] = []
    while i0 < i1 and j0 < j1 and compatible(a[i0], b[j0]):
        head.append((i0, j0))
        i0, j0 = i0 + 1, j0 + 1
    tail: list[tuple[int | None, int | None]] = []
    while i0 < i1 and j0 < j1 and compatible(a[i1 - 1], b[j1 - 1]):
        tail.append((i1 - 1, j1 - 1))
        i1, j1 = i1 - 1, j1 - 1
    n, m = i1 - i0, j1 - j0
    middle: list[tuple[int | None, int | None]] = []
    if n * m > MATCH_CELLS:
        middle = [(i0 + k, j0 + k) for k in range(min(n, m))]
        middle += [(i, None) for i in range(i0 + m, i1)] + [(None, j) for j in range(j0 + n, j1)]
    elif n and m:
        # Longest common subsequence under `compatible`.
        best = [[0] * (m + 1) for _ in range(n + 1)]
        for x in range(n - 1, -1, -1):
            for y in range(m - 1, -1, -1):
                best[x][y] = (
                    best[x + 1][y + 1] + 1 if compatible(a[i0 + x], b[j0 + y]) else max(best[x + 1][y], best[x][y + 1])
                )
        x = y = 0
        while x < n and y < m:
            if compatible(a[i0 + x], b[j0 + y]) and best[x][y] == best[x + 1][y + 1] + 1:
                middle.append((i0 + x, j0 + y))
                x, y = x + 1, y + 1
            elif best[x + 1][y] >= best[x][y + 1]:
                middle.append((i0 + x, None))
                x += 1
            else:
                middle.append((None, j0 + y))
                y += 1
        middle += [(i0 + k, None) for k in range(x, n)] + [(None, j0 + k) for k in range(y, m)]
    else:
        middle = [(i, None) for i in range(i0, i1)] + [(None, j) for j in range(j0, j1)]
    return head + middle + tail[::-1]


def align(a: list[Key], b: list[Key]) -> list[tuple[int | None, int | None]]:
    """Match old list elements to new ones: `(i, j)` is a pair, `(i, None)`
    removed, `(None, j)` added. Elements with a unique key anchor the
    match; between anchors, elements pair when no known field differs."""
    out: list[tuple[int | None, int | None]] = []
    i0 = j0 = 0
    for i, j in [*_anchors(a, b), (len(a), len(b))]:
        out += _stretch(a, b, i0, i, j0, j)
        if i < len(a):
            out.append((i, j))
        i0, j0 = i + 1, j + 1
    return out


def count_forced(v: Value, above: set[int] | None = None) -> int:
    """Forced values under `v`, derivations counted as one."""
    above = set() if above is None else above
    kind = v.type_name()
    if kind in UNEVALUATED:
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


def fully_forced(v: Value, above: set[int] | None = None) -> bool:
    """No thunk under `v`; a derivation counts as one value."""
    above = set() if above is None else above
    kind = v.type_name()
    if kind in UNEVALUATED:
        return False
    if kind not in {"attrs", "list"} or is_derivation(v):
        return True
    ident = v.identity()
    if ident in above:
        return True
    above.add(ident)
    try:
        if kind == "attrs":
            return all(fully_forced(v.value_at(i), above) for i in range(v.size()) if v.name_at(i) not in SKIP)
        return all(fully_forced(v.at(i), above) for i in range(v.length()))
    finally:
        above.discard(ident)


def has_failed(v: Value, above: set[int] | None = None) -> bool:
    """A value under `v` whose evaluation threw. Nix prints one as
    «thunk» (`print.cc`, `printFailed`), the same as a value nothing read."""
    above = set() if above is None else above
    kind = v.type_name()
    if kind == "failed":
        return True
    if kind not in {"attrs", "list"} or is_derivation(v):
        return False
    ident = v.identity()
    if ident in above:
        return False
    above.add(ident)
    try:
        if kind == "attrs":
            return any(has_failed(v.value_at(i), above) for i in range(v.size()) if v.name_at(i) not in SKIP)
        return any(has_failed(v.at(i), above) for i in range(v.length()))
    finally:
        above.discard(ident)


def show_lazy(v: Value) -> str:
    """`v` as far as it was evaluated, as Nix prints it: `«thunk»` marks
    what nothing needed. Forces nothing, so showing a value never widens
    what the next cascade compares. Nix prints a derivation as its path
    only when it may force (`print.cc`, `printDerivation`): modiff names a
    top-level one itself, by the attribute the walk keys it on. Not
    `drvPath` first, as Nix: the engine reads `drvPath` of each coerced
    derivation, and the name would then change after the first cascade."""
    if is_derivation(v):
        for name in DRV_KEYS:
            child = forced_attr(v, name)
            if child is not None and child.type_name() == "string":
                return f"«derivation {child.string_value()}»"
        return "«derivation»"
    return v.print_value(
        max_depth=6, max_attrs=PRINT_ITEMS, max_list_items=PRINT_ITEMS, max_string_length=4000, pretty_indent=2
    )


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
