"""One Nix evaluation: a fresh state, the arguments applied, plain data out.

Shared by the renderer and the ext_pillar. Each module imports huggorm's
bindings inside its functions: a minion forks job processes, and
libnixexpr with its collector must not load in the parent before the fork.
"""

import json
from collections.abc import Callable
from typing import Any

import salt.utils.json

#: `load(state, store)` returns the value to evaluate.
Load = Callable[[Any, Any], Any]


def evaluate(load: Load, args: dict[str, Any], store: str, what: str,
             error: type[Exception]) -> Any:
    """The value `load` gives, applied to `args` when it is a function.

    A function must name its arguments, as `{ grains, ... }:` does. A Nix
    error, and a function that names none, raise `error` with `what` in
    the message.
    """
    import huggorm_bindings
    from huggorm_bindings.errors import NixError

    try:
        return json.loads(_to_json(load, salt.utils.json.dumps(args), store, what, error))
    except NixError as e:
        raise error(f"{what}: {e}") from e
    finally:
        # Nix starts the collector with a 384 MiB heap, so it would not
        # collect for hundreds of evaluations and the process would grow
        # to that size. A collection with the state gone takes 0.5 ms and
        # keeps the process flat (havsorm#2).
        if huggorm_bindings.boehm_gc():
            huggorm_bindings.collect_garbage()


def _to_json(load: Load, args: str, store: str, what: str, error: type[Exception]) -> str:
    """Every Nix object made here is dead when this returns."""
    from huggorm_bindings import EvalState, Store

    nix_store = Store(store)
    state = EvalState(nix_store)
    value = load(state, nix_store)
    state.force(value)
    if value.type_name() == "function":
        from_json = state.eval_expr("builtins.fromJSON")
        try:
            value = value.apply_auto(from_json(state.make_string(args)))
        except ValueError as e:
            raise error(f"{what}: a Nix function names its arguments, as in "
                        "`{ grains, pillar, ... }:`") from e
    result: str = value.to_json()
    return result
