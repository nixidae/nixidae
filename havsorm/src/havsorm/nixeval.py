"""Nix evaluations: plain data out, from a fresh state or a warm one.

Shared by the renderer, the ext_pillar and the module. Each module
imports huggorm's bindings inside its functions: a minion forks job
processes, and libnixexpr with its collector must not load in the
parent before the fork.

A fresh state costs about 0.15 ms more than a reused one, so the state
itself is not worth keeping. Its cache of evaluated files is. Measured
over a file that holds `import <nixpkgs> { }`, a render that reads
`hello.drvPath` from it took 205 ms fresh and 9 ms warm.
"""

import json
import os
import threading
from collections.abc import Callable, Iterable
from typing import Any

import salt.utils.json

# libexpr's own in-memory files, such as derivation-internal.nix. No
# stat or fetch reaches them, and they never change.
_VIRTUAL = "«"


class Evaluator:
    """A fresh state for one evaluation."""

    def __init__(self, store: str) -> None:
        from huggorm_bindings import EvalState, Store

        self.store = Store(store)
        self.state = EvalState(self.store)

    def mount(self, client: Any, saltenv: str) -> str:
        """Show `saltenv`'s fileserver tree to the evaluator, through
        `client`. Returns the tree's path in the evaluator."""
        from havsorm.tree import SaltTree, mount_point

        root = self.state.mount(SaltTree(client, saltenv), path=mount_point(saltenv))
        path: str = self.store.print_store_path(root)
        return path

    def before(self) -> None:
        pass

    def after(self) -> None:
        pass


class Warm(Evaluator):
    """A state that outlives an evaluation, and keeps what it evaluated.

    Before each evaluation it stamps every cached file again: a file in
    a fileserver tree by the sha256 the fileserver serves now, any other
    file by its mtime and size. A file that changed, and a file cached
    with no stamp, make it forget the whole cache. Not only the file: an
    importer does not notice its import changing, and the cache holds no
    edge between the two (huggorm#83). A failed evaluation stamps
    nothing, so what it cached is forgotten next time, and a fixed file
    is read again rather than answering with the old error (huggorm#87).
    """

    def __init__(self, store: str) -> None:
        super().__init__(store)
        self.pid = os.getpid()
        self._trees: dict[str, Any] = {}
        self._stamps: dict[str, Any] = {}

    def mount(self, client: Any, saltenv: str) -> str:
        from havsorm.tree import SaltTree, mount_point

        path: str = self.store.print_store_path(mount_point(saltenv))
        tree = self._trees.get(path)
        if tree is None:
            tree = SaltTree(client, saltenv)
            self.state.mount(tree, path=mount_point(saltenv))
            self._trees[path] = tree
        else:
            # Here and not in `before`: until this render's client lists
            # the tree, the tree answers with what the last render read.
            tree.refresh(client)
            self._check(p for p in self._cached() if p.startswith(path + "/"))
        return path

    def _stamp(self, path: str) -> Any:
        for root, tree in self._trees.items():
            if path.startswith(root + "/"):
                return tree.digest(path[len(root):])
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _cached(self) -> list[str]:
        return [p for p in self.state.cached_files() if not p.startswith(_VIRTUAL)]

    def _check(self, paths: Iterable[str]) -> None:
        """Forget the whole cache if any of `paths` changed."""
        if any(p not in self._stamps or self._stamp(p) != self._stamps[p] for p in paths):
            for p in self._cached():
                self.state.forget_file(p)
            self._stamps.clear()

    def before(self) -> None:
        # The files in fileserver trees are checked when `mount` lists the
        # tree again.
        self._check(p for p in self._cached()
                    if not any(p.startswith(root + "/") for root in self._trees))

    def after(self) -> None:
        # `setdefault`: a file stamped before keeps that stamp, so an edit
        # that lands during an evaluation still differs at the next one.
        for p in self._cached():
            self._stamps.setdefault(p, self._stamp(p))


_local = threading.local()


def warm(store: str) -> Warm:
    """This thread's warm evaluator for `store`.

    One per thread, because a state answers to the thread that made it.
    Never one a fork inherited: the parent's collector threads do not
    exist in the child, so the child makes its own.
    """
    by_store: dict[str, Warm] = getattr(_local, "by_store", None) or {}
    _local.by_store = by_store
    found = by_store.get(store)
    if found is None or found.pid != os.getpid():
        found = by_store[store] = Warm(store)
    return found


#: `load(evaluator)` returns the value to evaluate.
Load = Callable[[Evaluator], Any]


def evaluate(load: Load, args: dict[str, Any], store: str, what: str,
             error: type[Exception], keep: bool = False) -> Any:
    """The value `load` gives, applied to `args` when it is a function.

    A function must name its arguments, as `{ grains, ... }:` does. A Nix
    error, and a function that names none, raise `error` with `what` in
    the message. With `keep`, this thread's warm evaluator does it.
    """
    import huggorm_bindings
    from huggorm_bindings.errors import NixError

    evaluator = warm(store) if keep else Evaluator(store)
    try:
        evaluator.before()
        out = _to_json(load, salt.utils.json.dumps(args), evaluator, what, error)
        evaluator.after()
        return json.loads(out)
    except NixError as e:
        raise error(f"{what}: {e}") from e
    finally:
        del evaluator
        # Nix starts the collector with a 384 MiB heap, so it would not
        # collect for hundreds of evaluations and the process would grow
        # to that size. A collection after each evaluation takes 0.5 ms
        # and keeps the process flat (havsorm#2).
        if huggorm_bindings.boehm_gc():
            huggorm_bindings.collect_garbage()


def _to_json(load: Load, args: str, evaluator: Evaluator, what: str,
             error: type[Exception]) -> str:
    """Every Nix value made here is dead when this returns."""
    state = evaluator.state
    value = load(evaluator)
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
