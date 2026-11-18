"""Which operation each request of the cloud API names. nixidae/nixidae#1408."""

from __future__ import annotations

import contextlib
import functools
import json
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import anyio
import pytest
from vivarium_agent import cloud
from vivarium_agent.cloud import Call, RouteError, route

if TYPE_CHECKING:
    from vivarium_agent.arpyc import AsyncConnection


@pytest.mark.parametrize(
    ("method", "target", "body", "call"),
    [
        ("GET", "/v1/self", b"", Call("self", {})),
        ("GET", "/v1/instances", b"", Call("list", {"tags": {}})),
        ("GET", "/v1/instances?tag:pool=x&other=y", b"", Call("list", {"tags": {"pool": "x"}})),
        ("GET", "/v1/instances/i-0001", b"", Call("describe", {"id": "i-0001"})),
        ("DELETE", "/v1/instances/i-0001", b"", Call("terminate", {"id": "i-0001"})),
        (
            "POST",
            "/v1/instances",
            b'{"template": "w", "tags": {"a": "b"}}',
            Call("launch", {"template": "w", "tags": {"a": "b"}}),
        ),
    ],
)
def test_each_route_names_its_operation(method: str, target: str, body: bytes, call: Call) -> None:
    assert route(method, target, body) == call


@pytest.mark.parametrize(
    ("method", "target", "body", "status"),
    [
        ("GET", "/v2/self", b"", HTTPStatus.NOT_FOUND),
        ("PUT", "/v1/instances", b"", HTTPStatus.METHOD_NOT_ALLOWED),
        ("POST", "/v1/instances", b"not json", HTTPStatus.BAD_REQUEST),
        ("POST", "/v1/instances", b'{"tags": {}}', HTTPStatus.BAD_REQUEST),
    ],
)
def test_a_request_that_names_no_operation_gets_its_status(method: str, target: str, body: bytes, status: int) -> None:
    with pytest.raises(RouteError) as caught:
        route(method, target, body)
    assert caught.value.status == status


class _Host:
    """The host's cloud behind the agent channel: it answers every call with 200."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def call(self, name: str, args: tuple[str, Any], kwargs: dict[str, Any]) -> tuple[int, Any]:
        del kwargs
        self.calls.append((name, args))
        return HTTPStatus.OK, {"op": args[0]}


async def _ask(path: Path, request: bytes) -> tuple[int, Any]:
    async with await anyio.connect_unix(path) as stream:
        await stream.send(request)
        reply = b""
        with contextlib.suppress(anyio.EndOfStream):
            while True:
                reply += await stream.receive()
    head, _, body = reply.partition(b"\r\n\r\n")
    return int(head.split(b" ")[1]), json.loads(body)


def test_the_socket_answers_and_survives_a_bad_request(tmp_path: Path) -> None:
    path = tmp_path / "cloud.sock"
    host = _Host()
    launch = b'{"template":"w"}'

    async def main() -> list[tuple[int, Any]]:
        async with anyio.create_task_group() as group:
            await group.start(functools.partial(cloud.serve, cast("AsyncConnection", host), str(path)))
            # A client that leaves before it sends anything.
            await (await anyio.connect_unix(path)).aclose()
            replies = [
                await _ask(path, b"BREW /v1/self HTTP/1.1\r\n\r\n"),
                await _ask(path, b"not http\r\n\r\n"),
                await _ask(
                    path, b"POST /v1/instances HTTP/1.1\r\nContent-Length: %d\r\n\r\n%s" % (len(launch), launch)
                ),
            ]
            group.cancel_scope.cancel()
        return replies

    replies = anyio.run(main)
    assert [status for status, _ in replies] == [HTTPStatus.METHOD_NOT_ALLOWED, HTTPStatus.BAD_REQUEST, HTTPStatus.OK]
    assert host.calls == [("cloud", ("launch", {"template": "w"}))]
