"""The `#!nix` renderer: an SLS file written in Nix.

The file evaluates to the highstate data, or to a function that takes
`{ grains, pillar, saltenv, sls, ... }` and returns it. A list of
single-key sets keeps its order, because a Nix attribute set has none.
"""

import json
import os
from typing import Any

import salt.utils.json
from salt.exceptions import SaltRenderError


def _ordered(data: Any) -> Any:
    if not isinstance(data, list):
        return data
    ordered: dict[str, Any] = {}
    for item in data:
        if not isinstance(item, dict) or len(item) != 1:
            raise SaltRenderError("a #!nix SLS list holds sets of one state each")
        ordered.update(item)
    return ordered


def render(template: Any, saltenv: str = "base", sls: str = "",
           tmplpath: str | None = None, **kws: Any) -> Any:
    # Imported here: a minion forks job processes, and libnixexpr with
    # its collector must not load in the parent before the fork.
    from huggorm_bindings import EvalState, Store
    from huggorm_bindings.errors import NixError

    text = template if isinstance(template, str) else template.read()
    base = os.path.dirname(tmplpath) if tmplpath else os.getcwd()
    store = (__opts__.get("havsorm") or {}).get("store", "dummy://")
    # The loader injects mappings that JSON cannot read, and it injects
    # `__pillar__` only when it was given one.
    args = salt.utils.json.dumps({
        "grains": dict(__grains__),
        "pillar": dict(globals().get("__pillar__") or {}),
        "saltenv": saltenv,
        "sls": sls,
    })
    try:
        state = EvalState(Store(store))
        value = state.eval_expr(text, base)
        state.force(value)
        if value.type_name() == "function":
            from_json = state.eval_expr("builtins.fromJSON")
            try:
                value = value.apply_auto(from_json(state.make_string(args)))
            except ValueError as e:
                raise SaltRenderError(
                    f"{sls}: a #!nix function names its arguments, as in "
                    "`{ grains, pillar, ... }:`") from e
        return _ordered(json.loads(value.to_json()))
    except NixError as e:
        raise SaltRenderError(str(e)) from e
