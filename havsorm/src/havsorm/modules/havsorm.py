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
import pathlib
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


def profile_target(profile: str) -> str | None:
    """The store path `profile` points at through its current generation,
    or None for a profile that does not exist.

    CLI Example:

        salt '*' havsorm.profile_target /nix/var/nix/profiles/salt
    """
    if not os.path.islink(profile):
        return None
    generation = os.path.join(os.path.dirname(profile), os.readlink(profile))
    return os.readlink(generation) if os.path.islink(generation) else None


def profile_set(profile: str, path: str, store: str | None = None) -> str:
    """Point `profile` at `path` through a new generation, as `nix ...
    --profile` does, and return the generation link. The store must
    hold `path`.

    CLI Example:

        salt '*' havsorm.profile_set /nix/var/nix/profiles/salt /nix/store/...-env
    """
    from huggorm_bindings.profiles import set_profile

    nix_store = _store(store)
    link = _call(path, lambda: set_profile(
        nix_store, pathlib.Path(profile), nix_store.parse_store_path(path)))
    return str(link)


def profile_generations(profile: str) -> list[dict[str, Any]]:
    """Every generation of `profile`, oldest first.

    CLI Example:

        salt '*' havsorm.profile_generations /nix/var/nix/profiles/salt
    """
    from huggorm_bindings.profiles import current_generation, find_generations

    current = current_generation(pathlib.Path(profile))
    return [{"number": g.number(), "link": str(g.path()), "created": g.creation_time(),
             "current": g.number() == current}
            for g in find_generations(pathlib.Path(profile))]


def profile_delete_generations(profile: str, older_than: str | None = None,
                               test: bool = False) -> list[int]:
    """Delete the generations of `profile` but the current one, or those
    older than `older_than`, such as `30d`, as `nix-env
    --delete-generations` does. Return the numbers deleted, or that would
    be with `test=True`.

    CLI Example:

        salt '*' havsorm.profile_delete_generations /nix/var/nix/profiles/salt 30d
    """
    from huggorm_bindings.profiles import (
        delete_generations_older_than,
        delete_old_generations,
        find_generations,
    )

    target = pathlib.Path(profile)
    before = {g.number() for g in find_generations(target)}
    if test:
        # Nix's dry run prints what it would delete, so compare a copy.
        return _would_delete(target, older_than)
    if older_than:
        _call(profile, lambda: delete_generations_older_than(target, older_than))
    else:
        _call(profile, lambda: delete_old_generations(target))
    return sorted(before - {g.number() for g in find_generations(target)})


def _would_delete(profile: pathlib.Path, older_than: str | None) -> list[int]:
    """What a deletion would remove, from a copy of the profile's links."""
    import shutil
    import tempfile

    from huggorm_bindings.profiles import (
        delete_generations_older_than,
        delete_old_generations,
        find_generations,
    )

    with tempfile.TemporaryDirectory() as scratch:
        copy = pathlib.Path(scratch) / profile.name
        for g in find_generations(profile):
            link = pathlib.Path(g.path())
            os.symlink(os.readlink(link), copy.with_name(link.name))
            shutil.copystat(link, copy.with_name(link.name), follow_symlinks=False)
        if profile.is_symlink():
            os.symlink(os.readlink(profile), copy)
        before = {g.number() for g in find_generations(copy)}
        if older_than:
            _call(str(profile), lambda: delete_generations_older_than(copy, older_than))
        else:
            _call(str(profile), lambda: delete_old_generations(copy))
        return sorted(before - {g.number() for g in find_generations(copy)})


def add_root(path: str, root: str, store: str | None = None) -> str:
    """Make `root` a symlink to `path` that keeps it from the collector.

    CLI Example:

        salt '*' havsorm.add_root /nix/store/...-hello /nix/var/nix/gcroots/salt/hello
    """
    nix_store = _store(store)
    link: str = _call(path, lambda: str(nix_store.add_perm_root(
        nix_store.parse_store_path(path), os.path.abspath(root))))
    return link


def store_path_of(path: str, store: str | None = None) -> str:
    """The store path that holds `path`, a file in a store object or the
    object itself.

    CLI Example:

        salt '*' havsorm.store_path_of /nix/store/...-nginx/bin/nginx
    """
    nix_store = _store(store)
    holder: str = _call(path, lambda: nix_store.print_store_path(
        nix_store.to_store_path(path).path()))
    return holder
