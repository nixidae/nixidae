"""Nix evaluation on a minion, as plain data.

    salt '*' havsorm.eval 'builtins.currentSystem'
    salt '*' havsorm.eval '{ grains, ... }: grains.os'

Named `havsorm`, because Salt's own `nix` module drives the `nix-env` CLI.
"""

import os
from typing import Any

from salt.exceptions import CommandExecutionError

from havsorm.nixeval import evaluate

# Salt's spelling for a function whose name is a Python builtin.
__func_alias__ = {"eval_": "eval"}


def eval_(expr: str, store: str | None = None) -> Any:
    """Evaluate `expr` and return its value.

    A function that names its arguments gets `{ grains, pillar, ... }`.
    A relative path names a file in the minion's working directory.

    CLI Example:

        salt '*' havsorm.eval '1 + 1'
    """
    store = store or (__opts__.get("havsorm") or {}).get("store", "dummy://")

    def load(state: Any, nix_store: Any) -> Any:
        return state.eval_expr(expr, os.getcwd())

    args = {"grains": dict(__grains__), "pillar": dict(globals().get("__pillar__") or {})}
    return evaluate(load, args, store, "havsorm.eval", CommandExecutionError)
