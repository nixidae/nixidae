"""Helpers for ttrpc protocol: build_response() function."""

from typing import Optional

from grpclib.const import Status
from ttrpc import Response, TtrpcStatus


def build_response(
    status: Status,
    message: Optional[str],
    payload: bytes,
) -> bytes:
    """Serialize a ttrpc Response frame payload."""
    ttrpc_status = TtrpcStatus(
        code=status.value,
        message=message or "",
    )
    response = Response(
        status=ttrpc_status,
        payload=payload,
    )
    return response.SerializeToString()
