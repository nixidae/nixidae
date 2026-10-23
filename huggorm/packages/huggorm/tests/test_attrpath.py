"""Attribute paths and auto-calls, as `nix --file` reads them.

`get` refuses a thunk, so each test selects through a member that is
still one: a walk that forgets to force fails here.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest

from huggorm.attrpath import (
    AttrPathError,
    AttrPathNotFoundError,
    AttrPathSearch,
    auto_call,
    flake_outputs,
    parse_attr_path,
    select_attr,
    select_attr_path,
    select_flake_attr,
    show_attr_paths,
)
from huggorm.session import AsyncSession
from huggorm_generated import AsyncEvalState, AsyncValue

URI = "dummy://"


@pytest.fixture
async def state() -> AsyncIterator[Any]:
    async with AsyncSession(URI) as session:
        yield session.eval(session.store())


async def text(state: AsyncEvalState, value: AsyncValue) -> str:
    await state.force(value)
    return await value.string_value()


@pytest.mark.parametrize(
    ("path", "parts"),
    [
        ("", ()),
        ("a", ("a",)),
        ("a.b", ("a", "b")),
        ('packages."x86_64-linux".hello', ("packages", "x86_64-linux", "hello")),
        ('"a.b"', ("a.b",)),
        ("a.", ("a",)),
        ("a..b", ("a", "", "b")),
    ],
)
def test_parse_splits_as_nix_does(path: str, parts: tuple[str, ...]) -> None:
    assert parse_attr_path(path) == parts


def test_parse_refuses_an_open_quote() -> None:
    with pytest.raises(AttrPathError, match="missing closing quote"):
        parse_attr_path('a."b')


async def test_select_forces_each_step(state: Any) -> None:
    root = await state.eval_expr('{ a = { b = "x" + "y"; }; }')
    assert await text(state, await select_attr(state, root, "a.b")) == "xy"


async def test_no_components_answers_the_value(state: Any) -> None:
    root = await state.eval_expr("{ a = 1; }")
    assert await select_attr_path(state, root, ()) is root


async def test_a_missing_attribute_names_what_is_there(state: Any) -> None:
    root = await state.eval_expr("{ a = { b = 1; c = 2; }; }")
    with pytest.raises(AttrPathNotFoundError, match=r"'z' not found.*b, c") as caught:
        await select_attr(state, root, "a.z")
    assert caught.value.depth == 1
    assert caught.value.available == ("b", "c")


async def test_a_step_into_a_non_set_is_not_found(state: Any) -> None:
    root = await state.eval_expr("{ a = 1; }")
    with pytest.raises(AttrPathNotFoundError) as caught:
        await select_attr(state, root, "a.b")
    assert caught.value.depth == 1


async def test_an_empty_component_is_refused(state: Any) -> None:
    root = await state.eval_expr("{ a = 1; }")
    with pytest.raises(AttrPathNotFoundError, match="empty component"):
        await select_attr(state, root, "a..b")


async def test_auto_call_fills_defaults(state: Any) -> None:
    f = await state.eval_expr('{ x ? "d" }: { y = x; }')
    called = await auto_call(state, f)
    assert await called.type_name() == "attrs"
    assert await text(state, await select_attr(state, called, "y")) == "d"


async def test_auto_call_follows_a_functor(state: Any) -> None:
    f = await state.eval_expr('{ v = "f"; __functor = self: { x ? self.v }: x; }')
    assert await text(state, await auto_call(state, f)) == "f"


@pytest.mark.parametrize("expr", ["{ a = 1; }", "x: x", "builtins.add"])
async def test_auto_call_answers_anything_else_as_it_is(state: Any, expr: str) -> None:
    value = await state.eval_expr(expr)
    assert await auto_call(state, value) is value


def test_show_attr_paths_joins_as_nix_does() -> None:
    assert show_attr_paths([]) == ""
    assert show_attr_paths(["a"]) == "'a'"
    assert show_attr_paths(["a", "b"]) == "'a' or 'b'"
    assert show_attr_paths(["a", "b", "c"]) == "'a', 'b' or 'c'"


BUILD = AttrPathSearch(
    prefixes=("packages.x.", "legacyPackages.x."), defaults=("packages.x.default",)
)


@pytest.mark.parametrize(
    ("fragment", "candidates"),
    [
        (None, ("packages.x.default",)),
        ("", ("packages.x.default",)),
        ("hello", ("packages.x.hello", "legacyPackages.x.hello", "hello")),
        (".hello", ("hello",)),
        (".", ("",)),
    ],
)
def test_candidates_follow_get_actual_attr_paths(
    fragment: str | None, candidates: tuple[str, ...]
) -> None:
    assert BUILD.candidates(fragment) == candidates


async def test_flake_outputs_drops_the_metadata(state: Any) -> None:
    flake = await state.eval_expr('{ outPath = "/x"; outputs = { a = "o"; }; }')
    outputs = await flake_outputs(state, flake)
    await state.force(outputs)
    assert await outputs.names() == ["a"]


async def test_select_flake_attr_takes_the_first_that_resolves(state: Any) -> None:
    outputs = await state.eval_expr('{ legacyPackages.x.hello = "l"; hello = "b"; }')
    value, path = await select_flake_attr(state, outputs, BUILD, "hello", flake_ref=".")
    assert path == "legacyPackages.x.hello"
    assert await text(state, value) == "l"


async def test_select_flake_attr_with_no_candidates_answers_the_outputs(state: Any) -> None:
    outputs = await state.eval_expr("{ }")
    assert await select_flake_attr(state, outputs, AttrPathSearch(), None, flake_ref=".") == (
        outputs,
        "",
    )


async def test_select_flake_attr_names_every_candidate_and_the_furthest_names(state: Any) -> None:
    outputs = await state.eval_expr("{ packages.x = { cowsay = 1; }; }")
    message = (
        r"flake '\.' does not provide attribute "
        r"'packages\.x\.hello', 'legacyPackages\.x\.hello' or 'hello'; "
        r"available attributes: cowsay"
    )
    with pytest.raises(AttrPathError, match=message):
        await select_flake_attr(state, outputs, BUILD, "hello", flake_ref=".")
