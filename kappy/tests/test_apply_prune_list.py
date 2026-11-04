"""The prune's LIST of one kind: what it retries, and for how long.

On a fresh cluster the first LIST of a kind whose CRD this apply created
answers 429 "storage is (re)initializing" (nixidae/nixidae#1176).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import httpx
import kr8s
import pytest
from kr8s.asyncio.objects import APIObject, get_class

from kappy.apply import PRUNE_LIST_RETRY_SECONDS, _list_for_prune

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from kr8s.asyncio import Api

    from kappy.apply import JsonValue


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def status(code: int, retry_after: int | None = None) -> kr8s.ServerError:
    body: dict[str, JsonValue] = {"kind": "Status", "message": "storage is (re)initializing", "code": code}
    if retry_after is not None:
        body["details"] = {"retryAfterSeconds": retry_after}
    return kr8s.ServerError(
        "storage is (re)initializing",
        response=httpx.Response(code, json=body, request=httpx.Request("GET", "http://api/x")),
    )


class _Lists:
    """`async_get` that raises each scripted error in turn, then lists one object."""

    namespace = "default"

    def __init__(self, *errors: kr8s.ServerError) -> None:
        self.errors = list(errors)
        self.calls = 0

    def async_get(self, *_args: object, **_kwargs: object) -> AsyncIterator[APIObject]:
        self.calls += 1
        errors = self.errors

        async def listed() -> AsyncIterator[APIObject]:
            if errors:
                raise errors.pop(0)
            yield get_class("ConfigMap", "v1")({"metadata": {"name": "a", "namespace": "ns"}}, api=cast("Api", self))

        return listed()


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


async def scan(api: _Lists, clock: _Clock) -> list[APIObject]:
    return await _list_for_prune(
        cast("Api", api),
        "ConfigMap",
        selector="ekn.dev/environment=airlab",
        kind="ConfigMap",
        sleep=clock.sleep,
        clock=clock,
    )


async def test_storage_initializing_is_listed_again_when_the_server_asks() -> None:
    api, clock = _Lists(status(429, retry_after=1), status(429, retry_after=2)), _Clock()

    listed = await scan(api, clock)

    assert [obj.name for obj in listed] == ["a"]
    assert clock.slept == [1.0, 2.0]


async def test_it_gives_up_at_the_bound() -> None:
    api, clock = _Lists(*[status(503) for _ in range(int(PRUNE_LIST_RETRY_SECONDS) + 5)]), _Clock()

    with pytest.raises(kr8s.ServerError):
        await scan(api, clock)

    assert clock.now <= PRUNE_LIST_RETRY_SECONDS


async def test_a_refusal_is_not_retried() -> None:
    api, clock = _Lists(status(403)), _Clock()

    with pytest.raises(kr8s.ServerError):
        await scan(api, clock)

    assert api.calls == 1
    assert clock.slept == []


async def test_a_kind_with_no_list_verb_is_empty() -> None:
    api, clock = _Lists(status(405)), _Clock()

    assert await scan(api, clock) == []
