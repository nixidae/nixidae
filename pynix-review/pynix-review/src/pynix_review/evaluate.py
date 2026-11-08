"""Evaluate nixpkgs attributes into a `Graph`, through huggorm.

Each call gets its own `EvalState` over its own `dummy://` store, so no
`.drv` reaches disk and two calls share nothing. The state shares the store
object it was given: `dummy://` opened twice is two empty stores, and a
`.drv` the state wrote would be missing from the second.
"""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio
import huggorm
from huggorm import errors

from .graph import Drv, Graph

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

SETTINGS: Final = {"allow-import-from-derivation": "false", "pure-eval": "false"}
# What CI's ci/eval/outpaths.nix passes, minus what only matters there.
CONFIG: Final = "{ allowAliases = false; allowUnfree = true; allowInsecurePredicate = _: true; }"


@dataclass(slots=True)
class Side:
    """One tree's evaluation: the closure walked, and what each attribute named."""

    graph: Graph = field(default_factory=dict)
    roots: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0
    walk_seconds: float = 0.0


def tree_expr(tree: str) -> str:
    """A Nix expression for a nixpkgs tree: a directory, or a tarball URL.

    The state that evaluates fetches a URL itself. A tree another process
    fetched is valid in the host store only, and newer Nix keeps it lazily
    in its git cache, not on disk, so a dummy store cannot read it."""
    if tree.startswith(("https://", "http://")):
        return f"(builtins.fetchTarball {json.dumps(tree)})"
    return str(Path(tree).resolve())


def pkgs_expr(tree: str, system: str, overlay: str | None) -> str:
    overlays = f"[ ({overlay}) ]" if overlay else "[ ]"
    return f"import {tree_expr(tree)} {{ system = {json.dumps(system)}; overlays = {overlays}; config = {CONFIG}; }}"


async def _read(store: huggorm.AsyncStore, base: str) -> Drv:
    parsed = await store.read_derivation(huggorm.StorePath(base))
    outputs = await parsed.outputs()
    fixed = [o for o in outputs.values() if isinstance(o, huggorm.DerivationOutputCAFixed)]
    addressed = {n: o for n, o in outputs.items() if isinstance(o, huggorm.DerivationOutputInputAddressed)}
    out = addressed.get("out") or next(iter(addressed.values()), None)
    return Drv(
        base=base,
        name=await parsed.name(),
        inputs=frozenset(await parsed.input_drvs()),
        fixed=tuple(str(o.ca()) for o in fixed) if fixed and len(fixed) == len(outputs) else None,
        out=str(out.path()) if out else None,
    )


async def _walk(store: huggorm.AsyncStore, graph: Graph, roots: Iterable[str]) -> None:
    """Read the closure under `roots` into `graph`, one breadth level at a time.

    Each read is four calls across huggorm's thread pool. A level's reads
    run at once, bounded by the pool (`huggorm.set_pool_size`). The fzf PR's
    8297 derivations walk in 3.3 s a side at pool size 1, 1.8 s at 4 and
    3.2 s at 16."""

    async def one(base: str, into: list[Drv]) -> None:
        into.append(await _read(store, base))

    level = {r for r in roots if r not in graph}
    while level:
        found: list[Drv] = []
        async with anyio.create_task_group() as tg:
            for base in level:
                tg.start_soon(one, base, found)
        for drv in found:
            graph[drv.base] = drv
        level = {i for drv in found for i in drv.inputs if i not in graph}


async def _resolve(state: huggorm.AsyncEvalState, pkgs: huggorm.AsyncValue, attr: str) -> huggorm.AsyncValue:
    value = pkgs
    for name in attr.split("."):
        value = await value.get(name)
        await state.force(value)
    return value


async def evaluate(tree: str, attrs: Sequence[str], *, system: str, overlay: str | None = None) -> Side:
    """Each attribute's `.drv`, and the closure under all of them.

    An attribute that fails to evaluate lands in `Side.failed` with the first
    line of its error; the rest go on."""
    side = Side()
    start = time.monotonic()
    async with contextlib.AsyncExitStack() as stack:
        store = huggorm.AsyncStore("dummy://?read-only=false")
        stack.push_async_callback(store.aclose)
        state = huggorm.AsyncEvalState(store, settings=SETTINGS)
        stack.push_async_callback(state.aclose)
        pkgs = await state.eval_expr(pkgs_expr(tree, system, overlay))
        await state.force(pkgs)
        for attr in attrs:
            try:
                drv = await (await _resolve(state, pkgs, attr)).drv_path()
            except errors.EvalBaseError as e:
                side.failed[attr] = str(e).splitlines()[0][:300]
                continue
            side.roots[attr] = drv.to_string()
        walk_start = time.monotonic()
        await _walk(store, side.graph, side.roots.values())
        side.walk_seconds = time.monotonic() - walk_start
    side.seconds = time.monotonic() - start
    return side
