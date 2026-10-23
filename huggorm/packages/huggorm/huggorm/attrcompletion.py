"""The attribute paths a caller pressing Tab could mean, as `nix` lists them.

`SourceExprCommand::completeInstallable` of `installables.cc` has two
rules, and they disagree:

- `--file` splits on the last literal dot and hands the left half back as
  typed, so `packages."a.b".he` keeps its quotation marks.
- a flake parses the whole path with `parseAttrPath` and rebuilds each
  candidate from the names it found, so the quotation marks are gone.

So there are two functions. Neither has a budget: a caller that completes
on a keypress gives up on its own clock.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from huggorm.attrpath import AttrPathNotFoundError, parse_attr_path, select_attr_path

if TYPE_CHECKING:
    from huggorm.attrpath import AttrPathSearch
    from huggorm_generated import AsyncEvalState, AsyncValue


async def _attr_names(state: AsyncEvalState, value: AsyncValue) -> tuple[str, ...]:
    """The names of `value`, or none: Nix guards with `v2.type() == nAttrs`."""
    await state.force(value)
    if await value.type_name() != "attrs":
        return ()
    return tuple(await value.names())


async def complete_file_attr_path(
    state: AsyncEvalState, root: AsyncValue, prefix: str
) -> list[str]:
    """The paths of `root` that `prefix` could mean: the `--file` branch.

    `root` is the file's value, already auto-called. A candidate is the
    whole path, because a shell replaces the whole word under the cursor.
    """
    head, separator, tail = prefix.rpartition(".")
    try:
        value = await select_attr_path(state, root, parse_attr_path(head)) if separator else root
    except AttrPathNotFoundError:
        return []
    lead = f"{head}." if separator else ""
    # Sorted and unique: Nix collects into the `std::set` of `Completions`.
    names = await _attr_names(state, value)
    return sorted({f"{lead}{name}" for name in names if name.startswith(tail)})


def _split_fragment(text: str) -> tuple[tuple[str, ...], str]:
    """The finished components of `text`, and the one being typed.

    A trailing dot finishes the component before it, so every name matches.
    """
    parts = list(parse_attr_path(text))
    if parts and not text.endswith("."):
        return tuple(parts[:-1]), parts[-1]
    return tuple(parts), ""


async def complete_flake_fragment(
    state: AsyncEvalState, outputs: AsyncValue, search: AttrPathSearch, fragment: str
) -> list[str]:
    """The fragments `fragment` could mean: `completeFlakeRefWithFragment`.

    `outputs` is the locked flake's output set. The answer is the union
    over every prefix of `search` and the top of the flake, each with its
    prefix removed. A leading `.` clears the prefixes and stays on each
    candidate. An empty fragment offers itself when a default of `search`
    resolves, because the caller may stop typing there.
    """
    root_mark = ""
    if fragment.startswith("."):
        fragment = fragment[1:]
        root_mark = "."
    prefixes = ("",) if root_mark else (*search.prefixes, "")

    found: list[str] = []
    for prefix in prefixes:
        stem, tail = _split_fragment(f"{prefix}{fragment}")
        depth = len(parse_attr_path(prefix))
        try:
            value = await select_attr_path(state, outputs, stem)
        except AttrPathNotFoundError:
            continue
        head = ".".join(stem[depth:])
        head = f"{head}." if head else ""
        names = await _attr_names(state, value)
        found.extend(f"{root_mark}{head}{name}" for name in names if name.startswith(tail))

    if not fragment:
        for default in search.defaults:
            try:
                await select_attr_path(state, outputs, parse_attr_path(default))
            except AttrPathNotFoundError:
                continue
            found.append(root_mark)
            break

    return sorted(set(found))
