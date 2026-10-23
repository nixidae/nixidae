"""Select an attribute path, and auto-call, the way `nix --file` does.

`get` and `has` refuse a thunk, so a walk forces each step through the
state first. The composition lives here once, rather than in every
consumer (nixidae/nixidae#1041).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from huggorm_bindings.errors import NixTypeError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from huggorm_generated import AsyncEvalState, AsyncValue

_MAX_SUGGESTED_ATTRS = 10


class AttrPathError(Exception):
    """An attribute path that does not parse, or does not select."""


class AttrPathNotFoundError(AttrPathError):
    """One attribute path did not resolve.

    `depth` counts the components that did resolve, so a caller that
    tries several candidates can report the one that went furthest.
    """

    def __init__(self, message: str, *, depth: int, available: Sequence[str]) -> None:
        super().__init__(message)
        self.depth = depth
        self.available = tuple(available)


def parse_attr_path(attrpath: str) -> tuple[str, ...]:
    """Split `attrpath` into components: `parseAttrPath` of `attr-path.cc`.

    A quoted component may hold a dot, so `packages."x86_64-linux".hello`
    is three components. An empty string gives none, and selecting none
    answers the value itself: what the `#.` fragment asks for.
    """
    parts: list[str] = []
    current = ""
    index = 0
    length = len(attrpath)
    while index < length:
        char = attrpath[index]
        if char == ".":
            parts.append(current)
            current = ""
        elif char == '"':
            index += 1
            while True:
                if index == length:
                    raise AttrPathError(f"missing closing quote in selection path {attrpath!r}")
                if attrpath[index] == '"':
                    break
                current += attrpath[index]
                index += 1
        else:
            current += char
        index += 1
    # Nix appends the last component only when it holds a character:
    # `a.` is one component.
    if current:
        parts.append(current)
    return tuple(parts)


def _describe(names: Sequence[str]) -> str:
    listed = ", ".join(names[:_MAX_SUGGESTED_ATTRS])
    suffix = "" if len(names) <= _MAX_SUGGESTED_ATTRS else f", ... ({len(names)} total)"
    return f"{listed}{suffix}"


async def select_attr_path(
    state: AsyncEvalState, value: AsyncValue, parts: Sequence[str]
) -> AsyncValue:
    """Select each component of `parts` in turn. The result is not forced.

    A missing component raises `AttrPathNotFoundError`, and so does a
    step into a value that is not an attribute set. Nix treats the two
    the same: `InstallableFlake::getCursors` catches the type error and
    tries the next candidate.
    """
    for depth, part in enumerate(parts):
        if not part:
            raise AttrPathNotFoundError(
                "attribute path contains an empty component", depth=depth, available=()
            )
        await state.force(value)
        try:
            present = await value.has(part)
        except NixTypeError as exc:
            raise AttrPathNotFoundError(str(exc), depth=depth, available=()) from exc
        if not present:
            names = await value.names()
            raise AttrPathNotFoundError(
                f"attribute {part!r} not found; available attributes: {_describe(names)}",
                depth=depth,
                available=names,
            )
        value = await value.get(part)
    return value


async def select_attr(state: AsyncEvalState, value: AsyncValue, attrpath: str) -> AsyncValue:
    """`select_attr_path` over a dotted path, quoted components and all."""
    return await select_attr_path(state, value, parse_attr_path(attrpath))


async def auto_call(state: AsyncEvalState, value: AsyncValue) -> AsyncValue:
    """Apply with no arguments, as `autoCallFunction` does for `--file`.

    A lambda with formals gets its defaults, and a `__functor` is
    followed. Anything else answers as it is, where `apply_auto`
    refuses: a file that is an attribute set is the ordinary case.
    The result is forced.
    """
    await state.force(value)
    kind = await value.type_name()
    if kind == "attrs" and await value.has("__functor"):
        functor = await value.get("__functor")
        await state.force(functor)
        return await auto_call(state, await functor(value))
    if kind == "function" and await value.is_lambda() and await value.has_formals():
        value = await value.apply_auto(await state.make_attrs())
        await state.force(value)
    return value
