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


def _evaluate(text: str, args: str, store: str, saltenv: str, sls: str,
              tmplpath: str | None) -> str:
    """The SLS as JSON. Every Nix object it makes is dead when it returns."""
    from huggorm_bindings import EvalState, Store

    nix_store = Store(store)
    state = EvalState(nix_store)
    if "cp.list_master" in __salt__:
        # Relative paths name files on the fileserver, not in the
        # minion's cache, which holds only this SLS.
        from havsorm.tree import SaltTree, mount_point, sls_dir

        root = state.mount(SaltTree(__salt__, saltenv), path=mount_point(saltenv))
        base = os.path.join(nix_store.print_store_path(root), sls_dir(sls, tmplpath))
    else:
        base = os.path.dirname(tmplpath) if tmplpath else os.getcwd()
    value = state.eval_expr(text, base.rstrip("/"))
    state.force(value)
    if value.type_name() == "function":
        from_json = state.eval_expr("builtins.fromJSON")
        try:
            value = value.apply_auto(from_json(state.make_string(args)))
        except ValueError as e:
            raise SaltRenderError(
                f"{sls}: a #!nix function names its arguments, as in "
                "`{ grains, pillar, ... }:`") from e
    result: str = value.to_json()
    return result


def render(template: Any, saltenv: str = "base", sls: str = "",
           tmplpath: str | None = None, **kws: Any) -> Any:
    # Imported here: a minion forks job processes, and libnixexpr with
    # its collector must not load in the parent before the fork.
    import huggorm_bindings
    from huggorm_bindings.errors import NixError

    text = template if isinstance(template, str) else template.read()
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
        return _ordered(json.loads(_evaluate(text, args, store, saltenv, sls, tmplpath)))
    except NixError as e:
        raise SaltRenderError(str(e)) from e
    finally:
        # Nix starts the collector with a 384 MiB heap, so it would not
        # collect for hundreds of renders and the minion would grow to
        # that size. A collection with the state gone takes 0.5 ms and
        # keeps the process flat (havsorm#2).
        if huggorm_bindings.boehm_gc():
            huggorm_bindings.collect_garbage()
