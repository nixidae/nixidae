"""`run`: a sync function on an affine object's own thread, as one call
(nixidae/nixidae#1556).

Hermetic: `dummy://` needs no daemon.
"""

from __future__ import annotations

import threading
import time
from collections.abc import AsyncIterator
from typing import Any

import anyio
import pytest
from conftest import runner_of

URI = "dummy://"
# `toJSON` calls `checkInterrupt` once per element.
INTERRUPTIBLE = "builtins.toJSON (builtins.genList (x: x) 2000)"
CANCEL_AFTER = 0.2
STOPPED_WITHIN = 5.0


@pytest.fixture
async def state() -> AsyncIterator[Any]:
    from huggorm_generated import AsyncEvalState, AsyncStore

    s = AsyncEvalState(AsyncStore(URI))
    yield s
    await s.aclose()


async def test_run_answers_what_the_function_returns(state: Any) -> None:
    assert await state.run(lambda s: s.eval_expr("1 + 2").integer()) == 3


async def test_run_is_on_the_objects_own_thread(state: Any) -> None:
    await state.eval_expr("1")
    ran = await state.run(lambda _: threading.current_thread().name)
    assert ran == runner_of(state).last_worker_name


async def test_a_value_runs_on_its_states_thread(state: Any) -> None:
    value = await state.eval_expr("{ a = 1; b = 2; }")
    names = await value.run(lambda v: [v.name_at(i) for i in range(v.size())])
    assert names == ["a", "b"]


async def test_run_refuses_a_sync_object(state: Any) -> None:
    with pytest.raises(TypeError, match="sync Value"):
        await state.run(lambda s: s.eval_expr("1"))


async def test_run_refuses_a_sync_object_inside_data(state: Any) -> None:
    with pytest.raises(TypeError, match="sync Value"):
        await state.run(lambda s: {"x": [1, (s.eval_expr("1"),)]})


async def test_the_functions_own_error_crosses_as_itself(state: Any) -> None:
    def fails(_: Any) -> None:
        raise LookupError("mine")

    with pytest.raises(LookupError, match="mine"):
        await state.run(fails)


async def test_a_nix_error_crosses_typed(state: Any) -> None:
    from huggorm_bindings.errors import NixError

    with pytest.raises(NixError):
        await state.run(lambda s: s.eval_expr('throw "no"').integer())


async def test_run_on_a_server_handle_refuses() -> None:
    from huggorm_generated import AsyncEvalState
    from huggorm_generated._runtime import Remote

    remote = AsyncEvalState._on(Remote(None, "not-a-handle"))  # type: ignore[arg-type]  # no call reaches the client
    with pytest.raises(TypeError, match="cannot cross to a server"):
        await remote.run(lambda s: None)


async def test_a_cancel_stops_run_at_its_next_nix_call(state: Any) -> None:
    calls = 0

    def forever(s: Any) -> None:
        nonlocal calls
        while True:
            s.eval_expr(INTERRUPTIBLE).to_json()
            calls += 1

    started = time.monotonic()
    with anyio.move_on_after(CANCEL_AFTER) as scope:
        await state.run(forever)
    assert scope.cancelled_caught
    assert calls > 0, "the function never ran a Nix call before the cancel"
    assert await state.run(lambda s: s.eval_expr("1 + 1").integer()) == 2
    assert time.monotonic() - started < STOPPED_WITHIN
