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

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qsl, urlsplit

if TYPE_CHECKING:
    from .arpyc import AsyncConnection

SOCKET = "/run/vivarium/cloud.sock"

_MAX_BODY = 1 << 20
"""User data is the largest field; EC2 caps its own at 16 KiB."""

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


async def _read_request(reader: asyncio.StreamReader) -> tuple[str, str, bytes]:
    head = await reader.readuntil(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    method, target, _version = lines[0].split(" ", 2)
    length = 0
    for line in lines[1:]:
        name, _, value = line.partition(":")
        if name.strip().lower() == "content-length":
            length = int(value.strip())
    if length > _MAX_BODY:
        raise RouteError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, f"a body of {length} bytes; the limit is {_MAX_BODY}")
    return method, target, await reader.readexactly(length)


def _response(reply: Reply) -> bytes:
    body = json.dumps(reply.body).encode()
    phrase = HTTPStatus(reply.status).phrase
    head = (
        f"HTTP/1.1 {reply.status} {phrase}\r\nContent-Type: application/json\r\n"
        f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n"
    )
    return head.encode() + body


async def _handle(conn: AsyncConnection, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        try:
            call = route(*await _read_request(reader))
            status, body = await conn.call("cloud", (call.op, call.args), {})
            reply = Reply(status, body)
        except RouteError as error:
            reply = Reply(error.status, {"error": str(error)})
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError) as error:
            reply = Reply(HTTPStatus.BAD_REQUEST, {"error": f"not an HTTP request: {error}"})
        except Exception as error:  # noqa: BLE001 -- the host's failure, carried back as vinegar's generic exception
            reply = Reply(HTTPStatus.BAD_GATEWAY, {"error": f"the host's cloud failed: {error}"})
        writer.write(_response(reply))
        await writer.drain()
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


async def serve(conn: AsyncConnection, path: str = SOCKET) -> asyncio.Server:
    """Answer the cloud API on *path*, through the host behind *conn*."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with contextlib.suppress(FileNotFoundError):
        os.unlink(path)
    return await asyncio.start_unix_server(lambda r, w: _handle(conn, r, w), path)
