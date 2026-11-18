"""Which operation each request of the cloud API names. nixidae/nixidae#1408."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from vivarium_agent.cloud import Call, RouteError, route


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
