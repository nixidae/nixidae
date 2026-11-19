"""Spike: which config values an evaluation forced, and what an extend changes.

Walks a module system's `config` without forcing it: a value whose type is
not `thunk` was forced. Then forces the same frontier in
`extendModules { modules = [ edit ]; }` and compares. Not a tool yet; see
the reply in the session that wrote it.
"""

from __future__ import annotations

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


def frontier(v: Value, path: str, seen: set[int], out: list[str]) -> None:
    kind = v.type_name()
    if kind == "thunk":
        return
    ident = v._identity()
    if ident in seen:
        return
    seen.add(ident)
    if kind == "attrs":
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
    kind = old.type_name()
    if kind == "thunk":
        return
    ident = old._identity()
    if ident in seen:
        return
    seen.add(ident)
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
    if kind == "attrs":
        old_names = set(old.names()) - SKIP
        new_names = set(new.names()) - SKIP
        for name in sorted(new_names - old_names):
            d.rows.append((Change.ADDED, f"{path}.{name}", "", "<unforced>"))
        for name in sorted(old_names - new_names):
            d.rows.append((Change.REMOVED, f"{path}.{name}", "", ""))
        for name in sorted(old_names & new_names):
            compare(state, old.get(name), new.get(name), f"{path}.{name}", seen, d)
    elif kind == "list":
        n_old, n_new = old.length(), new.length()
        if n_old != n_new:
            d.rows.append((Change.CHANGED, f"{path}.length", str(n_old), str(n_new)))
        for i in range(min(n_old, n_new)):
            compare(state, old.at(i), new.at(i), f"{path}[{i}]", seen, d)
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
    for row in d.rows[:40]:
        print(*row, sep="\t")
    print(
        f"derivations: base {len(base_drvs)}, ext {len(ext_drvs)}, only-ext {len(ext_drvs - base_drvs)}, only-base {len(base_drvs - ext_drvs)}"
    )
    for p in sorted(ext_drvs - base_drvs)[:20]:
        print("  +", p)


def main() -> None:
    state = EvalState(Store("dummy://" if sys.argv[1] == "toy" else "auto"))
    {"toy": toy, "nixos": nixos}[sys.argv[1]](state)


if __name__ == "__main__":
    main()
