"""Author the graph as a Nix expression, and evaluate it inside the planner.

The evaluator writes each derivation through the restricted socket, and the
planner submits the one the expression returns. A derivation finds its
inputs in the string context of its attributes. In the Nix language only
``builtins.storePath`` and ``builtins.appendContext`` attach context, and
both call ``EnsurePath``, which a ``builder-rpc-v0`` socket refuses. So the
arguments are built here, with huggorm, and carry their context already.

Needs the huggorm writer.
"""

from __future__ import annotations

import os
from typing import Any

from ._planner import Planner
from ._writer import HuggormWriter


def to_value(state: Any, data: Any, store_dir: str = "/nix/store") -> Any:
    """``data``, decoded from JSON, as a Nix value with its string context.

    A string under ``store_dir`` depends on its store path. A candidate,
    ``{ drv, outputs }`` as ``mkPlanner``'s ``candidate`` makes it, depends
    on the outputs it names: Nix builds a candidate only if a derivation
    the expression writes reads it.
    """
    # A context element names a store path by its base name.
    match data:
        case {"drv": str(drv), "outputs": dict(outputs)} if len(data) == 2:
            base = _base_name(drv)
            value = state.make_attrs()
            state.attrs_set(value, "drv", state.make_string(drv, [base]))
            paths = state.make_attrs()
            for name, path in outputs.items():
                state.attrs_set(paths, name, state.make_string(path, [f"!{name}!{base}"]))
            state.attrs_set(value, "outputs", paths)
            return value
        case dict():
            value = state.make_attrs()
            for name, item in data.items():
                state.attrs_set(value, name, to_value(state, item, store_dir))
            return value
        case list():
            value = state.make_list()
            for item in data:
                state.list_append(value, to_value(state, item, store_dir))
            return value
        case str() if data.startswith(store_dir + "/"):
            return state.make_string(data, [_base_name(data)])
        case str():
            return state.make_string(data)
        case bool():
            return state.make_bool(data)
        case int():
            return state.make_int(data)
        case float():
            return state.make_float(data)
        case None:
            return state.make_null()
    raise TypeError(f"no Nix value for {type(data).__name__}")


def _base_name(path: str) -> str:
    """``/nix/store/<hash>-<name>/sub`` is ``<hash>-<name>``."""
    return path.split("/")[3]


def main() -> None:
    """The planner of ``mkEvaluated``: apply ``expression`` to ``args``."""
    import huggorm

    planner = Planner.from_environ()
    if not isinstance(planner.writer, HuggormWriter):
        raise RuntimeError('an evaluated planner needs writer = "huggorm"')
    store = planner.writer.store
    state = huggorm.EvalState(store)

    expression = state.eval_file(planner.menu["expression"])
    root = expression(to_value(state, planner.menu["args"], os.environ["NIX_STORE"]))
    path = store.print_store_path(root.drv_path())
    planner.writer.submit(path, "out")
    print(f"pydd: submitted {path}")
