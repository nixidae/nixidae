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
    auto_call,
    parse_attr_path,
    select_attr,
    select_attr_path,
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
