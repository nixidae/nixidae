"""The `#!nix` renderer: an SLS file written in Nix.

The file evaluates to the highstate data, or to a function that takes
`{ grains, pillar, saltenv, sls, ... }` and returns it. A list of
single-key sets keeps its order, because a Nix attribute set has none.
"""

import os
from typing import Any

from salt.exceptions import SaltRenderError

from havsorm.nixeval import evaluate


def _ordered(data: Any) -> Any:
    if not isinstance(data, list):
        return data
    ordered: dict[str, Any] = {}
    for item in data:
        if not isinstance(item, dict) or len(item) != 1:
            raise SaltRenderError("a #!nix SLS list holds sets of one state each")
        ordered.update(item)
    return ordered


def _pillar_client(saltenv: str, tmplpath: str | None) -> Any:
    """A pillar file client, when `tmplpath` is in pillar_roots.

    Salt 3008 gives a pillar render its pillar client, and 3006 gives a
    renderer no client at all. Its cp functions there serve file_roots.
    A pillar SLS renders where pillar_roots is on disk, so the path says
    which tree the render belongs to.
    """
    if not tmplpath:
        return None
    path = os.path.realpath(tmplpath)
    for root in (__opts__.get("pillar_roots") or {}).get(saltenv, []):
        root = os.path.realpath(root)
        if os.path.commonpath([path, root]) == root:
            import salt.fileclient

            return salt.fileclient.get_file_client(__opts__, pillar=True)
    return None


def render(template: Any, saltenv: str = "base", sls: str = "",
           tmplpath: str | None = None, **kws: Any) -> Any:
    text = template if isinstance(template, str) else template.read()

    def load(state: Any, store: Any) -> Any:
        # A loader proxy, which reads as an empty mapping when the loader
        # was given no client.
        client = globals().get("__file_client__")
        if hasattr(client, "value"):
            client = client.value()
        if not hasattr(client, "file_list"):
            client = _pillar_client(saltenv, tmplpath)
        if client is None and "cp.list_master" in __salt__:
            from havsorm.tree import CpClient

            client = CpClient(__salt__)
        if client is not None:
            # Relative paths name files on the fileserver, not in the
            # minion's cache, which holds only this SLS.
            from havsorm.tree import SaltTree, mount_point, sls_dir

            root = state.mount(SaltTree(client, saltenv), path=mount_point(saltenv))
            base = os.path.join(store.print_store_path(root), sls_dir(sls, tmplpath))
        else:
            base = os.path.dirname(tmplpath) if tmplpath else os.getcwd()
        return state.eval_expr(text, base.rstrip("/"))

    # The loader injects mappings that JSON cannot read, and it injects
    # `__pillar__` only when it was given one.
    args = {
        "grains": dict(__grains__),
        "pillar": dict(globals().get("__pillar__") or {}),
        "saltenv": saltenv,
        "sls": sls,
    }
    store = (__opts__.get("havsorm") or {}).get("store", "dummy://")
    return _ordered(evaluate(load, args, store, sls, SaltRenderError))
