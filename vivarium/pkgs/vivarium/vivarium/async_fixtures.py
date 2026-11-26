"""Async pytest fixtures on a portal's loop, for a pytest that runs beside it.

Both vivarium plugins run pytest in a thread and the guests' connections
on a loop that a `BlockingPortal` reaches. An async fixture runs there
too, as a sync stand-in pytest can call.

An async generator fixture is one task: setup, the wait for its
teardown, and teardown. So a task group or cancel scope it holds across
`yield` works. Two portal calls, one per `__anext__`, make setup and
teardown two tasks; anyio then refuses to exit the scope, and the
portal stops (nixidae/nixidae#1413).

A contextvar a fixture sets does not reach the test: each runs in a
task of its own.
"""

from __future__ import annotations

import contextlib
import functools
import inspect
import math
from typing import TYPE_CHECKING, Any

import anyio

if TYPE_CHECKING:
    from collections.abc import Callable, Generator

    import pytest
    from anyio.abc import TaskStatus
    from anyio.from_thread import BlockingPortal


def is_async(func: Callable[..., Any]) -> bool:
    return inspect.iscoroutinefunction(func) or inspect.isasyncgenfunction(func)


def bound(func: Callable[..., Any], request: pytest.FixtureRequest) -> Callable[..., Any]:
    """*func* bound to the test's instance, as pytest binds a fixture
    method: pytest cannot bind the plain stand-in that `in_loop` returns."""
    instance = request.instance
    if instance is not None and inspect.ismethod(func) and type(func.__self__) is type(instance):
        method: Callable[..., Any] = func.__func__.__get__(instance)
        return method
    return func


def in_loop(
    portal: BlockingPortal,
    func: Callable[..., Any],
    *,
    deadline: float = math.inf,
    expired: Callable[[], BaseException] = TimeoutError,
) -> Callable[..., Any]:
    """A sync stand-in for the async fixture *func*. Its setup is
    cancelled at *deadline* (anyio's clock) and raises `expired()`; its
    teardown is not bounded, as a cancelled one would clean up nothing."""
    if inspect.isasyncgenfunction(func):

        def generator(**kwargs: Any) -> Generator[Any]:
            held, (value, finish) = portal.start_task(_hold, func, kwargs, deadline, expired)
            try:
                yield value
            finally:
                portal.call(finish.set)
                held.result()

        return generator

    def call(**kwargs: Any) -> Any:
        return portal.call(_once, functools.partial(func, **kwargs), deadline, expired)

    return call


async def _once(step: Callable[[], Any], deadline: float, expired: Callable[[], BaseException]) -> Any:
    with anyio.CancelScope(deadline=deadline):
        return await step()
    # Only this scope's deadline gets here: a cancel from outside propagates out of the `with`.
    raise expired()


async def _hold(
    func: Callable[..., Any],
    kwargs: dict[str, Any],
    deadline: float,
    expired: Callable[[], BaseException],
    *,
    task_status: TaskStatus[tuple[Any, anyio.Event]],
) -> None:
    finish = anyio.Event()
    with anyio.CancelScope(deadline=deadline) as scope:
        async with contextlib.asynccontextmanager(func)(**kwargs) as value:
            scope.deadline = math.inf
            task_status.started((value, finish))
            await finish.wait()
        return
    raise expired()
