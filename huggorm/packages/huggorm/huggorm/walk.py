"""
Value trees in one hop: a walk that reads a whole value, and a build
that makes one from Python data.

Both run on the value's own thread, so a tree of any size costs one
thread handover. The server runs them for a handle; `realize` and
`value` run them for an object in this process, and hand an object on
a server to its client.
"""

from __future__ import annotations

from typing import Any, assert_never

from huggorm_bindings.errors import NixError
from huggorm_generated._callspec import Accessor
from huggorm_generated._classes import CLASSES, DECLARED
from huggorm_generated._policy import BUILDERS, TREES
from huggorm_generated._runtime import (
    BaseRunner,
    Remote,
    _check_isolation,
    _materialize_args,
    unwrap_arg,
)
from huggorm_generated._shapes import Builds, Entries, Items, Leaf, Null, Scalar, Tree

from . import tree, views
from .codec import Node, encode_data, leaf
from .protocol import ProtocolError
from .remote import NixClient

# A realize with no bounds asked for. Small on purpose: every node the
# walk stops at costs a remote caller a lease, and a caller that wants
# more can say so.
DEFAULT_DEPTH = 8
DEFAULT_BUDGET = 1000


class TreeWalk:
    """One pass over a value that holds values, on that value's OWN
    thread.

    Everything it knows about the type comes from `spec`, which the
    binding declares and the build emits: which accessor says what
    a node is, which accessor reads each scalar kind, and how to reach
    the elements of a list or an attribute set. Each accessor is
    generated code that calls the method, so this class names no type
    and no method.

    It runs in ONE hop for the whole tree. A node per round trip would
    put a thread handover between every attribute, which is the cost
    the affine model exists to avoid paying repeatedly.

    Three things stop it, and all three produce the same answer - a
    proxy:

    - a kind the declaration does not name. That is a thunk, and a
      thunk is exactly what cannot be serialized;
    - a node already visited. Values are immutable and shared freely,
      so without this a diamond is copied twice and a cycle never
      ends. The repeated position carries a handle to the value
      instead of a copy. Two such positions get two handles: each
      stop is a fresh wrapper, and the table keys on the wrapper;
    - a node past the depth, or one the budget ran out on.

    A FORCING walk forces each node first, with the declared `force`.
    A throw keeps that node a proxy: Nix keeps the error in the value,
    so the caller's first read of it raises the same error, and the
    rest of the tree still arrives. An `Entries` node the declared
    `stop` answers true for stays a proxy too: a derivation, which
    forcing would instantiate.

    `depth` and `budget` of zero ask for the defaults.
    """

    def __init__(self, spec: Tree[Accessor], depth: int, budget: int, force: bool = False) -> None:
        self.spec = spec
        self.forcing = force and spec.force is not None
        self.depth = depth if depth > 0 else DEFAULT_DEPTH
        self.left = budget if budget > 0 else DEFAULT_BUDGET
        self.seen: set[Any] = set()
        self.truncated = False

    def _key(self, obj: Any) -> Any:
        """What makes two nodes the same node.

        Declared, because Python identity is not it wherever a binding
        builds a fresh wrapper per access: two wrappers over one object
        differ, and a wrapper that dies hands its id() to the next one -
        which reads as "already seen" and truncates a tree that was
        never visited."""
        how = self.spec.identity
        return how(obj) if how is not None else id(obj)

    def _stops(self, obj: Any) -> bool:
        """Whether a forcing walk keeps this node a proxy. A throw
        while asking means the same: the read raises it later."""
        stop = self.spec.stop
        if stop is None:
            return False
        try:
            return bool(stop(obj))
        except NixError:
            return True

    def node(self, obj: Any, depth: int) -> tree.Node:
        key = self._key(obj)
        # `depth` counts levels EXPANDED, so 1 is the root alone.
        if self.left <= 0 or depth >= self.depth or key in self.seen:
            self.truncated = True
            return tree.Stays(type(obj).__name__, obj)
        self.left -= 1
        self.seen.add(key)
        if self.forcing and (force := self.spec.force) is not None:
            try:
                force(obj)
            except NixError:
                return tree.Stays(type(obj).__name__, obj)
        match self.spec.kinds.get(self.spec.kind(obj)):
            case Leaf(wire=wire, read=read):
                return tree.Leaf(wire, read(obj))
            case Null():
                return tree.Leaf("null", None)
            case Items(size=size, item=item):
                return tree.Items(
                    type(obj).__name__, obj, [self.node(item(obj, i), depth + 1) for i in range(size(obj))]
                )
            case Entries(size=size, name=name, value=value):
                if self.forcing and self._stops(obj):
                    return tree.Stays(type(obj).__name__, obj)
                return tree.Entries(
                    type(obj).__name__,
                    obj,
                    {name(obj, i): self.node(value(obj, i), depth + 1) for i in range(size(obj))},
                )
            case None:
                # A kind nothing describes: it stays where it is.
                self.truncated = True
                return tree.Stays(type(obj).__name__, obj)


class Build:
    """Python data made into one value, on the state's OWN thread.

    Every method it calls is in `spec`, which the binding declares and
    the build emits, so this class names no type.

    Bottom-up: a Nix list or attribute set is sized when it is made, so
    a child is complete before its parent takes it. `given` holds the
    objects behind the handles the data named, resolved before the hop.

    It answers the tree it built, so a caller reads it with no further
    round trip. The root is always a handle: the caller asked for a
    value, and a bare scalar is not one."""

    def __init__(self, spec: Builds[Accessor], given: dict[str, Any]) -> None:
        self.spec = spec
        self.given = given

    @staticmethod
    def handles(raw: Any) -> list[str]:
        """Every handle id the data names."""
        match raw:
            case [Node.STAYS, str(hid)]:
                return [hid]
            case [Node.ITEMS, list(items)]:
                return [h for i in items for h in Build.handles(i)]
            case [Node.ENTRIES, dict(entries)]:
                return [h for v in entries.values() for h in Build.handles(v)]
        return []

    def root(self, state: Any, raw: Any) -> tree.Node:
        node, made = self.node(state, raw)
        if isinstance(node, tree.Leaf):
            return tree.Stays(type(made).__name__, made)
        return node

    def node(self, state: Any, raw: Any) -> tuple[tree.Node, Any]:
        """One data node, and the value made for it."""
        match raw:
            case [Node.LEAF, None]:
                made = self.spec.null(state)
                return tree.Leaf("null", None), made
            case [Node.LEAF, bool() | int() | float() | str() as v]:
                wire = Scalar(type(v).__name__)
                made = self.spec.leaves[wire](state, v)
                return tree.Leaf(wire, v), made
            case [Node.STAYS, str(hid)]:
                obj = self.given[hid]
                return tree.Stays(type(obj).__name__, obj), obj
            case [Node.ITEMS, list(items)]:
                made = self.spec.items(state)
                nodes = []
                for i in items:
                    n, m = self.node(state, i)
                    self.spec.add_item(state, made, m)
                    nodes.append(n)
                return tree.Items(type(made).__name__, made, nodes), made
            case [Node.ENTRIES, dict(entries)]:
                made = self.spec.entries(state)
                named = {}
                for k, v in entries.items():
                    n, m = self.node(state, v)
                    self.spec.add_entry(state, made, k, m)
                    named[k] = n
                return tree.Entries(type(made).__name__, made, named), made
        raise ProtocolError(f"a data node arrived as {raw!r:.80}")


def _declared(obj: Any) -> str:
    """The declared name of an async object. Through the MRO, because
    a realized view's class joins a mixin to the async class."""
    return next((DECLARED[c] for c in type(obj).__mro__ if c in DECLARED), "")


def _runner(obj: Any, what: str) -> BaseRunner:
    backend = getattr(obj, "_backend", None)
    if not isinstance(backend, BaseRunner):
        raise TypeError(f"{what} takes an async object in this process or on a server, not a {type(obj).__name__}")
    return backend


def _client(backend: Remote) -> NixClient:
    client = backend.client
    if not isinstance(client, NixClient):
        raise TypeError(f"a handle through a {type(client).__name__} has no realize or value")
    return client


def _out(node: tree.Node, runner: BaseRunner, passed: dict[int, Any]) -> Any:
    """A walked or built node as the remote client answers it: a scalar
    as itself, a list or an attribute set as a view, anything else as
    its async object. `passed` maps an object the data named to the
    async object the caller passed. An attribute set comes back sorted
    by name, which is the order Nix lists one in."""
    match node:
        case tree.Leaf():
            return leaf(node)
        case tree.Stays(cls=cls, obj=obj):
            # Not `passed.get(...) or`: an empty view is falsy.
            return passed[id(obj)] if id(obj) in passed else CLASSES[cls]._adopt(obj, runner)
        case tree.Items(cls=cls, obj=obj, items=items):
            held: Any = views.view_class(views.ListView, CLASSES[cls])._adopt(obj, runner)
            held._contents = [_out(i, runner, passed) for i in items]
            return held
        case tree.Entries(cls=cls, obj=obj, entries=entries):
            held = views.view_class(views.AttrsView, CLASSES[cls])._adopt(obj, runner)
            held._contents = {k: _out(entries[k], runner, passed) for k in sorted(entries)}
            return held
        case _:
            assert_never(node)


async def realize(obj: Any, depth: int = 0, budget: int = 0, force: bool = False) -> Any:
    """A whole value tree in one hop, on the value's own thread.

    The same call and answer as the remote client's `realize`, which
    an object on a server goes to: scalars as themselves, a list or an
    attribute set as a read-only view that is also the async object
    for its node, and every node the walk stopped at as its async
    object. In process a node costs what the sync binding costs, where
    one call per node costs a thread handover each."""
    if isinstance(backend := getattr(obj, "_backend", None), Remote):
        return await _client(backend).realize(obj, depth, budget, force)
    runner = _runner(obj, "realize")
    spec = TREES.get(_declared(obj))
    if spec is None:
        raise TypeError(f"a {type(obj).__name__} is not a value tree: its type declares no walk")
    walk = TreeWalk(spec, depth, budget, force)
    root = await runner.run(lambda target: walk.node(target, 0))
    return _out(root, runner, {})


async def value(state: Any, data: Any) -> Any:
    """A value `state` makes from Python data, in one hop on its own
    thread.

    The same call and answer as the remote client's `value`, which a
    state on a server goes to. Data is None, bool, int, float, str, a
    list or tuple, a mapping with str keys, or a value - a realized
    view included - at any depth. A value in the data comes back as
    the object that was passed. A value of another state is refused:
    its attribute names index another symbol table."""
    if isinstance(backend := getattr(state, "_backend", None), Remote):
        return await _client(backend).value(state, data)
    runner = _runner(state, "value")
    spec = BUILDERS.get(_declared(state))
    if spec is None:
        raise TypeError(f"a {type(state).__name__} makes no values: its type declares no builders")
    named: dict[str, Any] = {}

    def handle_id(obj: Any) -> str:
        key = str(len(named))
        named[key] = obj
        return key

    raw = encode_data(data, handle_id)
    _check_isolation(runner, named.values())
    await _materialize_args(list(named.values()))
    build = Build(spec, {key: unwrap_arg(obj) for key, obj in named.items()})
    root = await runner.run(lambda target: build.root(target, raw))
    return _out(root, runner, {id(build.given[key]): obj for key, obj in named.items()})
