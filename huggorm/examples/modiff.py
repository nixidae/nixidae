"""Spike: which config values an evaluation forced, and what an extend changes.

Walks a module system's `config` without forcing it: a value whose type is
not `thunk` was forced. Then forces the same frontier in
`extendModules { modules = [ edit ]; }` and compares. Not a tool yet; see
the reply in the session that wrote it.
"""

from __future__ import annotations

import cProfile
import difflib
import pstats
import sys
import time
from dataclasses import dataclass, field
from enum import StrEnum

from huggorm import EvalState, Store, Value

SKIP: frozenset[str] = frozenset({"_module"})


class Change(StrEnum):
    CHANGED = "changed"
    REMOVED = "removed"
    ADDED = "added"
    FAILED = "failed"


@dataclass(slots=True)
class Diff:
    rows: list[tuple[Change, str, str, str]] = field(default_factory=list)
    visited: int = 0
    forced_leaves: int = 0


def sel(state: EvalState, v: Value, *names: str) -> Value:
    for name in names:
        state.force(v)
        v = v.get(name)
    state.force(v)
    return v


def leaf(state: EvalState, v: Value) -> str:
    kind = v.type_name()
    match kind:
        case "int":
            return str(v.integer())
        case "float":
            return str(v.floating())
        case "bool":
            return str(v.boolean()).lower()
        case "string":
            return repr(str(v.string_value()))
        case "null":
            return "null"
        case _:
            return f"<{kind}>"


def forced_attr(v: Value, name: str) -> Value | None:
    if not v.has(name):
        return None
    child = v.get(name)
    return None if child.type_name() == "thunk" else child


def is_derivation(v: Value) -> bool:
    """An attrset with `outPath`, as `toString` reads it; `has` does not
    force. A derivation leads into nixpkgs through passthru and drvAttrs:
    3.9 M unforced children on a minimal NixOS config, none of them module
    config. Testing `type == "derivation"` misses the ones whose `type`
    nothing forced (1.18 M children left)."""
    return v.has("outPath")


def drv_attr(v: Value) -> str | None:
    """Which identifying attribute the evaluation forced: outPath, else name."""
    for name in ("outPath", "name"):
        if forced_attr(v, name) is not None:
            return name
    return None


def drv_key(state: EvalState, v: Value, name: str | None) -> str:
    """The derivation's `name` attribute, forced: the base forced it."""
    if name is None or not v.has(name):
        return "<derivation>"
    child = v.get(name)
    state.force(child)
    return f"<{name} {child.string_value()}>"


def align_key(state: EvalState, v: Value) -> str:
    """What a list element is matched on. Forces `v`, and a derivation's
    `name`: both are cheap, and an element the base forced is forced in the
    extend anyway."""
    state.force(v)
    kind = v.type_name()
    if kind == "attrs" and is_derivation(v):
        return drv_key(state, v, "name")
    if kind in {"attrs", "list"}:
        return f"<{kind}>"
    return leaf(state, v)


def frontier(v: Value, path: str, seen: set[int], out: list[str]) -> None:
    kind = v.type_name()
    if kind == "thunk":
        return
    ident = v._identity()
    if ident in seen:
        return
    seen.add(ident)
    if kind == "attrs" and is_derivation(v):
        out.append(path)
    elif kind == "attrs":
        for i in range(v.size()):
            name = v.name_at(i)
            if name not in SKIP:
                frontier(v.value_at(i), f"{path}.{name}", seen, out)
    elif kind == "list":
        for i in range(v.length()):
            frontier(v.at(i), f"{path}[{i}]", seen, out)
    else:
        out.append(path)


def compare(
    state: EvalState, old: Value, new: Value, path: str, seen: set[int], d: Diff
) -> None:
    """`seen` holds the containers on the current path only: enough to stop a
    cycle. A global set hid changes, because Nix shares one value for every
    `true`, so `{ p = true; q = true; }` walked `q` as already seen."""
    kind = old.type_name()
    if kind == "thunk":
        return
    if kind not in {"attrs", "list"}:
        _compare(state, old, new, kind, path, seen, d)
        return
    ident = old._identity()
    if ident in seen:
        return
    seen.add(ident)
    try:
        _compare(state, old, new, kind, path, seen, d)
    finally:
        seen.discard(ident)


def _compare(
    state: EvalState,
    old: Value,
    new: Value,
    kind: str,
    path: str,
    seen: set[int],
    d: Diff,
) -> None:
    d.visited += 1
    try:
        state.force(new)
    except Exception as err:
        d.rows.append((Change.FAILED, path, leaf(state, old), str(err).splitlines()[0]))
        return
    nkind = new.type_name()
    if kind != nkind:
        d.rows.append((Change.CHANGED, path, f"<{kind}>", f"<{nkind}>"))
        return
    if kind == "attrs" and is_derivation(old):
        d.forced_leaves += 1
        forced = drv_attr(old)
        a = drv_key(state, old, forced)
        b = drv_key(state, new, forced)
        if a != b:
            d.rows.append((Change.CHANGED, path, a, b))
    elif kind == "attrs":
        old_names = set(old.names()) - SKIP
        new_names = set(new.names()) - SKIP
        for name in sorted(new_names - old_names):
            d.rows.append((Change.ADDED, f"{path}.{name}", "", "<unforced>"))
        for name in sorted(old_names - new_names):
            d.rows.append((Change.REMOVED, f"{path}.{name}", "", ""))
        for name in sorted(old_names & new_names):
            compare(state, old.get(name), new.get(name), f"{path}.{name}", seen, d)
    elif kind == "list":
        olds = [old.at(i) for i in range(old.length())]
        news = [new.at(i) for i in range(new.length())]
        if any(v.type_name() == "thunk" for v in olds):
            if len(olds) != len(news):
                d.rows.append(
                    (Change.CHANGED, f"{path}.length", str(len(olds)), str(len(news)))
                )
            for i, (a, b) in enumerate(zip(olds, news, strict=False)):
                compare(state, a, b, f"{path}[{i}]", seen, d)
            return
        a_keys = [align_key(state, v) for v in olds]
        b_keys = [align_key(state, v) for v in news]
        matcher = difflib.SequenceMatcher(a=a_keys, b=b_keys, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                    compare(state, olds[i], news[j], f"{path}[{i}]", seen, d)
                continue
            for i in range(i1, i2):
                d.rows.append((Change.REMOVED, f"{path}[{i}]", a_keys[i], ""))
            for j in range(j1, j2):
                d.rows.append((Change.ADDED, f"{path}[+{j}]", "", b_keys[j]))
    else:
        d.forced_leaves += 1
        a, b = leaf(state, old), leaf(state, new)
        if a != b:
            d.rows.append((Change.CHANGED, path, a, b))


TOY = """
let
  lib = import /etc/nixpkgs/lib;
  base = lib.evalModules {
    modules = [
      ({ lib, config, ... }: {
        options = {
          a = lib.mkOption { type = lib.types.int; default = 1; };
          b = lib.mkOption { type = lib.types.int; };
          c = lib.mkOption { type = lib.types.str; default = "untouched"; };
          nested = lib.mkOption { type = lib.types.attrsOf lib.types.int; default = { x = 1; y = 2; }; };
          drv = lib.mkOption { type = lib.types.package; };
        };
        config.b = config.a * 10;
        config.drv = derivation {
          name = "probe-${toString config.b}";
          system = "x86_64-linux";
          builder = "/bin/sh";
        };
      })
    ];
  };
in { inherit base; ext = base.extendModules { modules = [ { a = 2; } ]; }; }
"""

NIXOS = """
let
  base = import /etc/nixpkgs/nixos/lib/eval-config.nix {
    system = "x86_64-linux";
    modules = [{
      boot.loader.grub.enable = false;
      fileSystems."/" = { device = "/dev/null"; fsType = "ext4"; };
      system.stateVersion = "25.11";
    }];
  };
in { inherit base; ext = base.extendModules { modules = [ { services.openssh.enable = true; } ]; }; }
"""


def toy(state: EvalState) -> None:
    top = state.eval_expr(TOY, "/")
    config = sel(state, top, "base", "config")
    nested = sel(state, config, "nested")
    print(
        "nested forced alone:", {n: nested.get(n).type_name() for n in nested.names()}
    )
    sel(state, config, "drv", "name")
    paths: list[str] = []
    frontier(config, "config", set(), paths)
    print("frontier:", paths)
    econfig = sel(state, top, "ext", "config")
    d = Diff()
    compare(state, config, econfig, "config", set(), d)
    for row in d.rows:
        print(*row, sep="\t")
    print("ext c:", econfig.get("c").type_name())


def nixos(state: EvalState) -> None:
    t0 = time.monotonic()
    top = state.eval_expr(NIXOS, "/")
    state.set_record_derivations(True)
    config = sel(state, top, "base", "config")
    sel(state, config, "system", "build", "toplevel", "drvPath")
    t1 = time.monotonic()
    base_drvs = set(state.recorded_out_paths())
    state.clear_recorded_derivations()
    paths: list[str] = []
    frontier(config, "config", set(), paths)
    t2 = time.monotonic()
    econfig = sel(state, top, "ext", "config")
    d = Diff()
    compare(state, config, econfig, "config", set(), d)
    t3 = time.monotonic()
    ext_drvs = set(state.recorded_out_paths())
    print(f"base eval (toplevel.drvPath): {t1 - t0:.2f}s")
    print(f"frontier walk: {t2 - t1:.2f}s, {len(paths)} forced leaves")
    print(
        f"extend + replay + compare: {t3 - t2:.2f}s, {d.visited} values, {d.forced_leaves} leaves"
    )
    by = {c: sum(1 for r in d.rows if r[0] == c) for c in Change}
    print("rows:", by)
    for row in d.rows:
        print(*row, sep="\t")
    print(
        f"derivations: base {len(base_drvs)}, ext {len(ext_drvs)}, only-ext {len(ext_drvs - base_drvs)}, only-base {len(base_drvs - ext_drvs)}"
    )
    for p in sorted(ext_drvs - base_drvs)[:20]:
        print("  +", p)


def profile(state: EvalState) -> None:
    top = state.eval_expr(NIXOS, "/")
    config = sel(state, top, "base", "config")
    sel(state, config, "system", "build", "toplevel", "drvPath")
    econfig = sel(state, top, "ext", "config")
    t0 = time.monotonic()
    compare(state, config, econfig, "config", set(), Diff())
    t1 = time.monotonic()
    compare(state, config, econfig, "config", set(), Diff())
    t2 = time.monotonic()
    print(
        f"compare, forcing ext: {t1 - t0:.2f}s; compare again, all forced: {t2 - t1:.2f}s"
    )
    prof = cProfile.Profile()
    prof.enable()
    compare(state, config, econfig, "config", set(), Diff())
    prof.disable()
    pstats.Stats(prof).sort_stats("tottime").print_stats(15)


def bench(state: EvalState) -> None:
    big = state.eval_expr(
        'builtins.listToAttrs (builtins.genList (i: { name = "a${toString i}"; value = i; }) 20000)',
        "/",
    )
    n = big.size()
    cases = {
        "type_name": lambda i: big.value_at(i).type_name(),
        "value_at": lambda i: big.value_at(i),
        "name_at": lambda i: big.name_at(i),
        "get": lambda i: big.get(f"a{i}"),
        "_identity": lambda i: big.value_at(i)._identity(),
        "integer": lambda i: big.value_at(i).integer(),
    }
    for label, fn in cases.items():
        t0 = time.perf_counter()
        for i in range(n):
            fn(i)
        print(f"{label:10} {(time.perf_counter() - t0) / n * 1e6:7.2f} us/call")
    t0 = time.perf_counter()
    for _ in range(100):
        big.names()
    print(
        f"names()    {(time.perf_counter() - t0) / 100 * 1e3:7.2f} ms per 20000-attr set"
    )


def census(state: EvalState) -> None:
    top = state.eval_expr(NIXOS, "/")
    config = sel(state, top, "base", "config")
    sel(state, config, "system", "build", "toplevel", "drvPath")
    thunks: dict[str, int] = {}
    seen: set[int] = set()

    def walk(v: Value, path: str) -> None:
        if v._identity() in seen:
            return
        seen.add(v._identity())
        match v.type_name():
            case "attrs" if is_derivation(v):
                pass
            case "attrs":
                for i in range(v.size()):
                    child = v.value_at(i)
                    if child.type_name() == "thunk":
                        thunks[path] = thunks.get(path, 0) + 1
                    else:
                        walk(child, f"{path}.{v.name_at(i)}")
            case "list":
                for i in range(v.length()):
                    child = v.at(i)
                    if child.type_name() == "thunk":
                        thunks[path] = thunks.get(path, 0) + 1
                    else:
                        walk(child, f"{path}[]")
            case _:
                pass

    t0 = time.monotonic()
    walk(config, "config")
    print(
        f"census walk {time.monotonic() - t0:.2f}s, {len(seen)} forced values, {sum(thunks.values())} thunk children"
    )
    for path, count in sorted(thunks.items(), key=lambda kv: -kv[1])[:25]:
        print(f"{count:8} {path}")


def main() -> None:
    state = EvalState(Store("dummy://" if sys.argv[1] == "toy" else "auto"))
    {"toy": toy, "nixos": nixos, "profile": profile, "bench": bench, "census": census}[
        sys.argv[1]
    ](state)


if __name__ == "__main__":
    main()
