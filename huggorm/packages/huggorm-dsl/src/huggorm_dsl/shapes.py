"""The shapes a declaration and the runtime share.

A declaration builds `Tree[str]`, naming each accessor. The emitter
writes the same classes into `_policy`, and the server builds
`Tree[Accessor]` with each accessor resolved. pygen copies this file
into the generated package as `_shapes.py`, so the generated package
never imports huggorm-dsl at run time.

The standard library only, and Python 3.11 syntax: this file runs in
both places, and the generated package runs on 3.11.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, TypeVar

A = TypeVar("A")


# nixidae/nixidae#622.
class Subscription(StrEnum):
    """What a call does to a subscription a server shares."""

    # Opens one subscription per object, or per process for a free
    # function, and answers each caller its own reader of it.
    OPEN = "open"
    # Leaves only the readers this caller opened.
    CLOSE = "close"


class Scalar(StrEnum):
    """A builtin the wire carries as one field."""

    STR = "str"
    # A signed 64-bit integer: every int64_t and half of a uint64_t.
    INT = "int"
    # A uint64_t. Above the boundary it is an `int`: the width is a fact
    # about the crossing, not about the type a caller sees
    # (nixidae/nixidae#616).
    UINT = "uint"
    FLOAT = "float"
    BOOL = "bool"
    # A store holds files, and their contents are not text.
    BYTES = "bytes"

    @property
    def leaf(self) -> bool:
        """Whether a value tree's leaf may be one."""
        return self in (Scalar.STR, Scalar.INT, Scalar.FLOAT, Scalar.BOOL)

    @property
    def python(self) -> str:
        """The Python type it is above the boundary."""
        return "int" if self is Scalar.UINT else self.value


@dataclass(frozen=True, slots=True)
class Leaf(Generic[A]):
    """A node that crosses as one scalar. `wire` is its declared type,
    which picks the arm of the value message; `read` answers it."""

    wire: Scalar
    read: A


@dataclass(frozen=True, slots=True)
class Null:
    """A node that crosses as None. It has nothing to read."""


@dataclass(frozen=True, slots=True)
class Items(Generic[A]):
    """A node that holds values by position: `size` counts them and
    `item(i)` reads one."""

    size: A
    item: A


@dataclass(frozen=True, slots=True)
class Entries(Generic[A]):
    """A node that holds values by name: `size` counts them, and
    `name(i)` and `value(i)` read one."""

    size: A
    name: A
    value: A


@dataclass(frozen=True, slots=True)
class Tree(Generic[A]):
    """How a value that HOLDS other values is walked.

    Declared next to the binding, because only the declaration knows
    which of a type's methods answers its kind and which reads a
    child. The server walks a whole tree in one hop, so it needs all
    of this before it starts.

    `kinds` maps each answer of the `kind` accessor to how that node
    is read. An answer it does not hold is a thunk or something else
    the declaration does not name, and it crosses as a proxy.

    `identity` is what makes two nodes the same node, and it is
    declared rather than assumed: Python identity is not it wherever a
    binding builds a fresh wrapper per access. None falls back to
    `id()`.

    `force` forces a node in place, for a walk that forces. `stop` is a
    predicate on an `Entries` node that keeps it a proxy in such a
    walk: a derivation, which a forcing walk must not enter. None means
    the type declares neither."""

    kind: A
    kinds: dict[str, Leaf[A] | Items[A] | Entries[A] | Null]
    identity: A | None = None
    force: A | None = None
    stop: A | None = None


@dataclass(frozen=True, slots=True)
class Builds(Generic[A]):
    """How Python data becomes a value, by the methods of the type that
    makes values.

    `null` takes no argument. `leaves` maps a tree leaf's wire type to
    the method that makes one from that scalar. `items` makes an empty
    list and `add_item(list, value)` appends to it; `entries` and
    `add_entry(attrs, name, value)` do the same for an attribute set."""

    null: A
    leaves: dict[Scalar, A]
    items: A
    add_item: A
    entries: A
    add_entry: A
