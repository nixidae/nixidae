"""
Collections over the wire, and the whole tree in one round trip.

Every element of a value is a value in its own right, so walking an
attribute set from a client is a chain of handles - a round trip and a
thread handover per node. Realize walks it once, on the value's own
thread, and answers with the shape (nixidae/nixidae#567).
"""

import gc
import time
from decimal import Decimal
from typing import Any

import anyio
import pytest
from conftest import Server

from huggorm import remote
from huggorm.views import AttrsView, ListView
from huggorm.walk import realize, value
from huggorm_bindings.errors import NixError
from huggorm_generated import AsyncEvalState, AsyncStore, AsyncValue, _runtime
from huggorm_generated._runtime import InternalError


def held(value: Any) -> bool:
    """A node the walk did not expand: a handle, and not a view. A
    view is a handle too, so `isinstance(v, AsyncValue)` cannot tell."""
    return isinstance(value, AsyncValue) and not isinstance(value, AttrsView | ListView)


@pytest.fixture(params=["server", "in-process"])
async def state(request: pytest.FixtureRequest) -> Any:
    """A fresh evaluator per test, on a server and in this process.
    `realize` and `value` answer the same for both, so each test holds
    both to one set of assertions. A server state has its own
    connection, so one test's handles never outlive it into another's
    assertions."""
    if request.param == "in-process":
        s = AsyncEvalState(AsyncStore("dummy://"))
        yield s
        await s.aclose()
        return
    server: Server = request.getfixturevalue("server")
    async with remote.connect(server.path) as c:
        s = await c.acquire(AsyncEvalState, await c.acquire(AsyncStore, "dummy://"))
        yield s
        await s.aclose()


@pytest.fixture
async def served(server: Server) -> Any:
    """A server state alone, for what only a connection has."""
    async with remote.connect(server.path) as c:
        s = await c.acquire(AsyncEvalState, await c.acquire(AsyncStore, "dummy://"))
        yield s
        await s.aclose()


async def bag(state: Any) -> Any:
    """{ apple = "first"; xs = [ 7 ]; zebra = 1; }"""
    attrs = await state.make_attrs()
    await state.attrs_set(attrs, "zebra", await state.make_int(1))
    await state.attrs_set(attrs, "apple", await state.make_string("first"))
    xs = await state.make_list()
    await state.list_append(xs, await state.make_int(7))
    await state.attrs_set(attrs, "xs", xs)
    return attrs


# -- walking it one call at a time -----------------------------------------


async def test_an_attribute_set_crosses_as_a_proxy(state: Any) -> None:
    attrs = await bag(state)
    assert await attrs.type_name() == "attrs"
    assert await attrs.size() == 3


async def test_attribute_names_come_back_alphabetical(state: Any) -> None:
    """Nix attribute sets are alphabetical, and the order survives the
    wire because it is the storage order, not a detail of the
    message."""
    attrs = await bag(state)
    names = [await attrs.name_at(i) for i in range(3)]
    assert names == ["apple", "xs", "zebra"], names


async def test_an_attribute_value_is_a_handle_of_its_own(state: Any) -> None:
    attrs = await bag(state)
    assert await (await attrs.get("apple")).string_value() == "first"
    assert await (await (await attrs.get("xs")).at(0)).integer() == 7


# -- one round trip --------------------------------------------------------


async def test_realize_returns_the_shape(state: Any) -> None:
    tree = await realize(await bag(state))
    assert tree == {"apple": "first", "xs": [7], "zebra": 1}, tree
    assert list(tree) == ["apple", "xs", "zebra"], "still alphabetical"


async def test_a_float_realizes_as_a_float(state: Any) -> None:
    """A float in a tree crosses as a float. With no scalar arm for
    it, it would cross as a proxy, and the shape would hold a handle."""
    tree = await realize(await state.eval_expr("{ x = 0.5; n = 2; }"))
    assert tree == {"n": 2, "x": 0.5}, tree
    assert isinstance(tree["x"], float)


async def test_a_null_realizes_as_none(state: Any) -> None:
    tree = await realize(await state.eval_expr("{ x = null; }"))
    assert tree == {"x": None}, tree


async def test_realize_forces_nothing(state: Any) -> None:
    """A thunk is exactly what cannot be serialized, so it crosses as a
    proxy and the caller forces it with the call that already exists."""
    lazy = await state.make_attrs()
    await state.attrs_set(lazy, "later", await state.parse_expr("42"))
    got = await realize(lazy)
    assert held(got["later"]), type(got["later"]).__name__

    await state.force(got["later"])
    assert await realize(lazy) == {"later": 42}


async def test_depth_bounds_the_walk(state: Any) -> None:
    """depth counts levels EXPANDED, so 1 is the root alone. Zero asks
    for the server's default."""
    attrs = await bag(state)

    flat = await realize(attrs, depth=1)
    assert set(flat) == {"apple", "xs", "zebra"}, flat
    assert all(held(v) for v in flat.values()), flat

    shallow = await realize(attrs, depth=2)
    assert shallow["zebra"] == 1
    assert shallow["apple"] == "first"
    assert held(shallow["xs"][0]), shallow


async def test_budget_bounds_the_walk_sideways(state: Any) -> None:
    """The bound that actually bites: an attribute set can hold a
    hundred thousand entries one level down, which no depth limit
    touches."""
    wide = await state.make_attrs()
    for i in range(20):
        await state.attrs_set(wide, f"k{i:02d}", await state.make_int(i))

    # 1 for the root, then 3 children before it runs out.
    capped = await realize(wide, budget=4)
    kept = [k for k, v in capped.items() if not held(v)]
    assert len(kept) == 3, kept
    assert await capped["k19"].integer() == 19, "the rest is still reachable"


async def test_a_repeated_value_crosses_once(state: Any) -> None:
    """Values are immutable and shared freely, so without visit
    tracking a diamond is copied and a cycle never ends. The repeated
    position carries a handle instead of a second copy."""
    shared = await state.make_list()
    twice = await state.make_attrs()
    await state.attrs_set(twice, "a", shared)
    await state.attrs_set(twice, "b", shared)

    diamond = await realize(twice)
    expanded = [k for k, v in diamond.items() if not held(v)]
    assert expanded == ["a"] and diamond["a"] == [], diamond


# -- a forcing walk (nixidae/nixidae#721) ------------------------------------------


async def test_a_forcing_walk_forces_what_it_visits(state: Any) -> None:
    value = await state.eval_expr('{ a = 1 + 1; b = [ (2 * 3) ]; d = { e = "x"; }; }')

    lazy = await realize(value)
    assert held(lazy["a"]), "a plain walk forces nothing"

    assert await realize(value, force=True) == {"a": 2, "b": [6], "d": {"e": "x"}}


async def test_a_throw_stays_a_handle_and_raises_on_read(state: Any) -> None:
    """Nix keeps a thrown force in the value, so the walk goes on and
    the caller's first read of that node raises the same error."""
    value = await state.eval_expr('{ ok = 1; bad = throw "boom"; }')

    tree = await realize(value, force=True)
    assert tree["ok"] == 1
    assert held(tree["bad"])
    with pytest.raises(NixError, match="boom"):
        await state.force(tree["bad"])


async def test_a_forcing_walk_does_not_enter_a_derivation(state: Any) -> None:
    value = await state.eval_expr('{ drv = { type = "derivation"; name = "x"; };  other = { type = "other"; }; }')

    tree = await realize(value, force=True)
    assert held(tree["drv"]), tree["drv"]
    assert tree["other"] == {"type": "other"}


async def test_an_endless_value_ends_at_the_budget(state: Any) -> None:
    """Every level is a fresh value, so the visit set never stops it;
    the budget does. forceValueDeep would never return."""
    value = await state.eval_expr("let f = n: { n = n; next = f (n + 1); }; in f 0")

    tree = await realize(value, force=True, budget=20)
    node, levels = tree, 0
    # The budget can run out inside a level, so the last `n` may be a
    # handle too.
    while isinstance(node, AttrsView) and not held(node["n"]):
        assert node["n"] == levels
        node, levels = node["next"], levels + 1
    assert 5 < levels < 20, levels


# -- views (nixidae/nixidae#721) ---------------------------------------------------


async def apply_int(state: Any, fn: str, arg: Any) -> int:
    result = await (await state.eval_expr(fn))(arg)
    await state.force(result)
    return int(await result.integer())


async def test_a_view_reads_locally_and_passes_back(state: Any) -> None:
    """Reads are local; handing the view to Nix sends its handle."""
    tree = await realize(await state.eval_expr("{ a = 1; d = { e = 2; }; }"), force=True)

    assert isinstance(tree, AttrsView) and isinstance(tree["d"], AttrsView)
    assert tree["d"]["e"] == 2 and len(tree) == 2 and "a" in tree
    assert await apply_int(state, "x: x.d.e + x.a", tree) == 3
    assert await apply_int(state, "x: x.e * 10", tree["d"]) == 20


async def test_a_child_view_outlives_its_root(served: Any) -> None:
    """The root's handle goes when its last view does, and the server
    keeps it while a child lives."""
    client = served._backend.client
    tree = await realize(await served.eval_expr("{ d = { e = 2; }; }"), force=True)
    child = tree["d"]
    del tree
    gc.collect()
    assert await client.flush_dropped() >= 1, "the root was released"

    assert await apply_int(served, "x: x.e + 1", child) == 3


async def test_a_view_copies_to_a_plain_dict(state: Any) -> None:
    tree = await realize(await state.eval_expr("{ a = 1; }"), force=True)

    merged = tree | {"b": 2}
    assert type(merged) is dict and merged == {"a": 1, "b": 2}
    assert type(dict(tree)) is dict and dict(tree) == {"a": 1}


async def test_a_list_view(state: Any) -> None:
    xs = await realize(await state.eval_expr("[ 1 2 3 ]"), force=True)

    assert isinstance(xs, ListView)
    assert xs == [1, 2, 3] and xs[1:] == [2, 3] and len(xs) == 3
    assert await apply_int(state, "builtins.length", xs) == 3


# -- data into Nix (nixidae/nixidae#721) --------------------------------------------


@pytest.mark.parametrize(
    ("data", "kind"), [(None, "null"), (True, "bool"), (1, "int"), (0.5, "float"), ("s", "string")]
)
async def test_a_scalar_becomes_a_value(state: Any, data: Any, kind: str) -> None:
    """A scalar at the root answers as a handle: the caller asked for a
    value. `True` is a bool, never the int it is to isinstance."""
    made = await value(state, data)
    assert held(made)
    assert await made.type_name() == kind


async def test_data_becomes_a_value_in_one_round_trip(state: Any) -> None:
    data = {"a": 1, "xs": [1, 2, None], "d": {"e": "x"}, "t": (True,)}

    made = await value(state, data)
    assert isinstance(made, AttrsView) and isinstance(made["xs"], ListView)
    assert made == data | {"t": [True]}
    assert await apply_int(state, "x: x.a + builtins.length x.xs", made) == 4
    assert await apply_int(state, "x: builtins.stringLength x.e", made["d"]) == 1
    assert await realize(made, force=True) == made


async def test_a_handle_in_the_data_comes_back_as_itself(state: Any) -> None:
    """Including an empty view, which is falsy."""
    inner = await realize(await state.eval_expr("{ e = 2; }"), force=True)
    empty = await value(state, [])

    made = await value(state, {"inner": inner, "n": 1, "z": empty})
    assert made["inner"] is inner and made["z"] is empty
    assert await apply_int(state, "x: x.inner.e + x.n + builtins.length x.z", made) == 3


@pytest.mark.parametrize(
    ("data", "error", "match"),
    [
        (Decimal(1), TypeError, "Decimal has no Nix value"),
        ({1}, TypeError, "set has no Nix value"),
        ({1: 2}, TypeError, "attribute name is a str"),
        (lambda x: x, TypeError, "make_primop"),
        (2**63, OverflowError, "64 bits"),
    ],
)
async def test_data_nix_has_no_value_for_is_refused(state: Any, data: Any, error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        await value(state, [data])


async def test_data_that_holds_itself_is_refused(state: Any) -> None:
    loop: list[Any] = []
    loop.append(loop)
    with pytest.raises(ValueError, match="holds itself"):
        await value(state, loop)


async def test_a_value_of_another_state_is_refused(served: Any) -> None:
    client = served._backend.client
    other = await client.acquire(AsyncEvalState, await client.acquire(AsyncStore, "dummy://"))
    foreign = await other.make_int(1)
    with pytest.raises(InternalError) as refused:
        await client.value(served, [foreign])
    assert isinstance(refused.value.__cause__, TypeError)
    assert "another EvalState" in str(refused.value.__cause__)
    await other.aclose()


async def test_in_process_a_value_of_another_state_is_refused_before_the_hop() -> None:
    """In process the refusal reaches the caller as itself: it runs
    before the hop, which would wrap it."""
    state = AsyncEvalState(AsyncStore("dummy://"))
    other = AsyncEvalState(AsyncStore("dummy://"))
    foreign = await other.make_int(1)
    with pytest.raises(TypeError, match="another EvalState"):
        await value(state, [foreign])
    await other.aclose()
    await state.aclose()


async def test_a_server_value_in_in_process_data_is_refused(served: Any) -> None:
    state = AsyncEvalState(AsyncStore("dummy://"))
    with pytest.raises(TypeError, match="handle on a server"):
        await value(state, [await served.make_int(1)])
    await state.aclose()


async def test_realize_refuses_what_is_not_an_async_object() -> None:
    with pytest.raises(TypeError, match="takes an async object"):
        await realize({"a": 1})


async def test_in_process_realize_and_value_are_one_hop_each(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of them: a tree of any size is one call on the state's
    thread, not one per node."""
    state = AsyncEvalState(AsyncStore("dummy://"))
    tree = await state.eval_expr("builtins.genList (i: { inherit i; }) 50")
    hops: list[str] = []
    real = _runtime._hop

    async def counted(executor: Any, label: str, fn: Any) -> Any:
        hops.append(label)
        return await real(executor, label, fn)

    monkeypatch.setattr(_runtime, "_hop", counted)
    assert await realize(tree, force=True, budget=200) == [{"i": i} for i in range(50)]
    assert await value(state, [{"i": i} for i in range(50)]) == [{"i": i} for i in range(50)]
    assert hops == ["run", "run"]
    monkeypatch.undo()
    await state.aclose()


# `toJSON` calls `checkInterrupt` once per element, and `filter` forces
# a million of them, so this runs far longer than the cancel waits.
ENDLESS = "{ x = builtins.length (builtins.filter (x: builtins.toJSON (builtins.genList (y: y) 2000) != \"\") (builtins.genList (x: x) 1000000)); }"


async def test_a_cancel_stops_a_forcing_walk(state: Any) -> None:
    value_ = await state.eval_expr(ENDLESS)
    started = time.monotonic()
    with anyio.move_on_after(0.2) as scope:
        await realize(value_, force=True)
    assert scope.cancelled_caught
    assert time.monotonic() - started < 5.0
    assert await realize(await state.eval_expr("{ a = 1 + 1; }"), force=True) == {"a": 2}
