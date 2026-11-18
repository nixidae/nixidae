"""vivarium's cloud API, as a guest sees it: HTTP on a Unix socket. nixidae/nixidae#1408.

The guest's agent serves `SOCKET`. Each request crosses the agent channel
to the host, whose session starts and stops guests from templates. The
channel exists on every backend, offline and in a Nix sandbox, so the API
needs no guest network. `curl --unix-socket /run/vivarium/cloud.sock
http://cloud/v1/instances` reads it by hand.

    POST   /v1/instances       {"template", "tags", "user_data"} -> 202, the instance
    GET    /v1/instances       ?tag:<key>=<value> ... -> 200, {"instances": [...]}
    GET    /v1/instances/<id>  -> 200, the instance
    DELETE /v1/instances/<id>  -> 202, the instance
    GET    /v1/self            -> 200, the calling guest as an instance, or 404

An instance is `{"id", "template", "state", "tags", "launched_by", "error",
"notice"}`. `state` goes `pending`, `running`, then `terminated`, or
`failed`. A guest's own poweroff terminates its instance; a crash of its
own makes it `failed`, with the exit in `error`. A launch the run has no
room for answers 409, as a cloud out of capacity does.
"""

from __future__ import annotations

import contextlib
import functools
import json
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

import anyio
from anyio.streams.buffered import BufferedByteStream

if TYPE_CHECKING:
    from anyio.abc import SocketStream, TaskStatus

    from .arpyc import AsyncConnection

SOCKET = "/run/vivarium/cloud.sock"

_MAX_BODY = 1 << 20
"""User data is the largest field; EC2 caps its own at 16 KiB."""

_MAX_HEAD = 1 << 16
"""The request line and headers, as asyncio's stream limit allowed them."""

type Json = dict[str, Any]
"""A JSON object as the API carries it; each operation reads its own fields."""


@dataclass(frozen=True, slots=True)
class Call:
    """One request, as the host's cloud answers it."""

    op: str
    args: Json


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: Json


class RouteError(ValueError):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


def route(method: str, target: str, body: bytes) -> Call:
    """The operation an HTTP request names. Pure; `RouteError` for one that names none."""
    url = urlsplit(target)
    parts = [part for part in url.path.split("/") if part]
    if parts[:1] != ["v1"]:
        raise RouteError(HTTPStatus.NOT_FOUND, f"no route {url.path}; the API is under /v1")
    rest = parts[1:]
    if rest == ["self"] and method == "GET":
        return Call("self", {})
    if rest == ["instances"] and method == "GET":
        tags = {key.removeprefix("tag:"): value for key, value in parse_qsl(url.query) if key.startswith("tag:")}
        return Call("list", {"tags": tags})
    if rest == ["instances"] and method == "POST":
        try:
            args = json.loads(body or b"{}")
        except json.JSONDecodeError as error:
            raise RouteError(HTTPStatus.BAD_REQUEST, f"the body is no JSON: {error}") from None
        if not isinstance(args, dict) or not isinstance(args.get("template"), str):
            raise RouteError(HTTPStatus.BAD_REQUEST, 'a launch needs {"template": "<name>"}')
        return Call("launch", args)
    if len(rest) == 2 and rest[0] == "instances" and method in ("GET", "DELETE"):
        return Call("describe" if method == "GET" else "terminate", {"id": rest[1]})
    raise RouteError(HTTPStatus.METHOD_NOT_ALLOWED, f"no {method} on {url.path}")


async def _read_request(reader: BufferedByteStream) -> tuple[str, str, bytes]:
    head = await reader.receive_until(b"\r\n\r\n", _MAX_HEAD)
    lines = head.decode("latin-1").split("\r\n")
    method, target, _version = lines[0].split(" ", 2)
    length = 0
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip())
    if length > _MAX_BODY:
        raise RouteError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"a body of {length} bytes; the limit is {_MAX_BODY}")
    return method, target, await reader.receive_exactly(length)


def _response(reply: Reply) -> bytes:
    body = json.dumps(reply.body).encode()
    phrase = HTTPStatus(reply.status).phrase
    head = (
        f"HTTP/1.1 {reply.status} {phrase}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode() + body


async def _handle(conn: AsyncConnection, client: SocketStream) -> None:
    async with client:
        stream = BufferedByteStream(client)
        try:
            call = route(*await _read_request(stream))
            status, body = await conn.call("cloud", (call.op, call.args), {})
            reply = Reply(status, body)
        except RouteError as error:
            reply = Reply(error.status, {"error": str(error)})
        except (anyio.IncompleteRead, anyio.DelimiterNotFound, ValueError) as error:
            reply = Reply(HTTPStatus.BAD_REQUEST, {"error": f"not an HTTP request: {error}"})
        except Exception as error:  # noqa: BLE001 -- the host's failure, carried back as vinegar's generic exception
            reply = Reply(HTTPStatus.BAD_GATEWAY, {"error": f"the host's cloud failed: {error}"})
        # A client that left takes its reply with it. The listener's task
        # group would end the agent on an error a handler lets out.
        with contextlib.suppress(OSError, anyio.BrokenResourceError, anyio.ClosedResourceError):
            await stream.send(_response(reply))


async def serve(
    conn: AsyncConnection, path: str = SOCKET, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED
) -> None:
    """Answer the cloud API on *path*, through the host behind *conn*, until cancelled.

    `task_status.started()` comes once *path* accepts connections.
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).unlink(missing_ok=True)
    async with await anyio.create_unix_listener(path) as listener:
        task_status.started()
        await listener.serve(functools.partial(_handle, conn))
