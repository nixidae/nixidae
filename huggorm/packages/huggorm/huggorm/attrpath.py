"""Select an attribute path, and auto-call, the way `nix --file` does.

`get` and `has` refuse a thunk, so a walk forces each step through the
state first. The composition lives here once, rather than in every
consumer (nixidae/nixidae#1041).
"""

from __future__ import annotations

from dataclasses import dataclass
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


async def flake_outputs(state: AsyncEvalState, flake: AsyncValue) -> AsyncValue:
    """The `outputs` of a called flake: what every `nix` command resolves against.

    `call_flake` answers the outputs merged with the flake's metadata
    (`inputs`, `narHash`, `outPath`, `sourceInfo`...). `openEvalCache` of
    `flake.cc` selects `outputs` first; skipping it accepts `#outPath`
    where `nix` reports the flake does not provide it.
    """
    return await select_attr(state, flake, "outputs")


def show_attr_paths(paths: Sequence[str]) -> str:
    """Quote and join `paths` as `showAttrPaths` of `installable-flake.cc` does.

    `'a'`, `'a' or 'b'`, `'a', 'b' or 'c'`.
    """
    quoted = [f"'{path}'" for path in paths]
    if len(quoted) <= 1:
        return "".join(quoted)
    return ", ".join(quoted[:-1]) + " or " + quoted[-1]


@dataclass(frozen=True, slots=True)
class AttrPathSearch:
    """The attribute paths one command tries, in order.

    These are `getDefaultFlakeAttrPathPrefixes` and `getDefaultFlakeAttrPaths`
    of `SourceExprCommand`; `nix develop` puts `devShells` first, `nix run`
    puts `apps` first. `prefixes` apply to a fragment, `defaults` to none.
    Both empty means a fragment selects exactly what it says.
    """

    prefixes: tuple[str, ...] = ()
    defaults: tuple[str, ...] = ()

    def candidates(self, fragment: str | None) -> tuple[str, ...]:
        """Every path to try for `fragment`, best first.

        `InstallableFlake::getActualAttrPaths`: no fragment gives the
        defaults; a leading `.` is stripped and the rest is the only
        candidate; any other fragment goes under each prefix, then bare.
        """
        if not fragment:
            return self.defaults
        if fragment.startswith("."):
            return (fragment[1:],)
        return (*(f"{prefix}{fragment}" for prefix in self.prefixes), fragment)


async def select_flake_attr(
    state: AsyncEvalState,
    outputs: AsyncValue,
    search: AttrPathSearch,
    fragment: str | None,
    *,
    flake_ref: str,
) -> tuple[AsyncValue, str]:
    """The first candidate of `search` that resolves, and its path.

    None resolving raises `AttrPathError` with the message of
    `InstallableFlake::getCursors`, plus the names where the furthest
    candidate stopped: a list of paths alone does not say what the
    flake does provide.
    """
    candidates = search.candidates(fragment)
    if not candidates:
        return outputs, ""
    furthest: AttrPathNotFoundError | None = None
    for candidate in candidates:
        try:
            return await select_attr(state, outputs, candidate), candidate
        except AttrPathNotFoundError as exc:
            if furthest is None or exc.depth > furthest.depth:
                furthest = exc
    message = f"flake {flake_ref!r} does not provide attribute {show_attr_paths(candidates)}"
    if furthest is not None and furthest.available:
        message = f"{message}; available attributes: {_describe(furthest.available)}"
    raise AttrPathError(message)


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
