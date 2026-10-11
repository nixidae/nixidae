"""Nix on a minion: evaluation, and the store the states change.

    salt '*' havsorm.eval 'builtins.currentSystem'
    salt '*' havsorm.realise /nix/store/...-hello-2.12.1

Named `havsorm`, because Salt's own `nix` module drives the `nix-env` CLI.

Configuration, in the minion config:

    havsorm:
      eval_store: dummy://   # what an evaluation may read and write
      store: auto            # the store the states change
      nix_settings:          # nix.conf settings for this process
        substituters: https://cache.nixos.org
"""

import os
from typing import Any

from salt.exceptions import CommandExecutionError

from havsorm.nixeval import evaluate

# Salt's spelling for a function whose name is a Python builtin.
__func_alias__ = {"eval_": "eval"}


def _config() -> dict[str, Any]:
    config: dict[str, Any] = __opts__.get("havsorm") or {}
    return config


def eval_(expr: str, store: str | None = None) -> Any:
    """Evaluate `expr` and return its value.

    A function that names its arguments gets `{ grains, pillar, ... }`.
    A relative path names a file in the minion's working directory.

    CLI Example:

        salt '*' havsorm.eval '1 + 1'
    """
    store = store or _config().get("eval_store", "dummy://")

    def load(state: Any, nix_store: Any) -> Any:
        return state.eval_expr(expr, os.getcwd())

    args = {"grains": dict(__grains__), "pillar": dict(globals().get("__pillar__") or {})}
    return evaluate(load, args, store, "havsorm.eval", CommandExecutionError)


def _store(store: str | None) -> Any:
    """The store the states change, with `nix_settings` applied first.

    Nix settings are global to the process. A job runs in a process of
    its own by default, so one job's settings reach no other job.
    """
    import huggorm_bindings

    for name, value in (_config().get("nix_settings") or {}).items():
        huggorm_bindings.set_setting(name, value if isinstance(value, str) else str(value))
    return huggorm_bindings.Store(store or _config().get("store", "auto"))


def _call(what: str, fn: Any) -> Any:
    from huggorm_bindings.errors import NixError

    try:
        return fn()
    except NixError as e:
        raise CommandExecutionError(f"{what}: {e}") from e


def is_realised(path: str, store: str | None = None) -> bool:
    """Whether the store holds `path`.

    CLI Example:

        salt '*' havsorm.is_realised /nix/store/...-hello-2.12.1
    """
    nix_store = _store(store)
    valid: bool = _call(path, lambda: nix_store.is_valid_path(nix_store.parse_store_path(path)))
    return valid


def realise(path: str, store: str | None = None) -> str:
    """Make `path` valid in the store, from a substituter. It never builds:
    a minion takes what a builder made.

    CLI Example:

        salt '*' havsorm.realise /nix/store/...-hello-2.12.1
    """
    nix_store = _store(store)
    _call(path, lambda: nix_store.ensure_path(nix_store.parse_store_path(path)))
    return path


def add_root(path: str, root: str, store: str | None = None) -> str:
    """Make `root` a symlink to `path` that keeps it from the collector.

    CLI Example:

        salt '*' havsorm.add_root /nix/store/...-hello /nix/var/nix/gcroots/salt/hello
    """
    nix_store = _store(store)
    link: str = _call(path, lambda: str(nix_store.add_perm_root(
        nix_store.parse_store_path(path), os.path.abspath(root))))
    return link
