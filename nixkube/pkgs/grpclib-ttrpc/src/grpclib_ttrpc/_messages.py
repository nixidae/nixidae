"""Helpers for ttrpc protocol: build_response() function."""

from typing import Optional

from grpclib.const import Status as gStatus
from ttrpc.proto.status_pb2 import Status as ttStatus
from ttrpc.ttrpc_pb2 import Response


def build_response(
    status: gStatus,
    message: Optional[str],
    payload: bytes,
) -> bytes:
    """Serialize a ttrpc Response frame payload."""
    ttrpc_status = ttStatus(
        code=status.value,
        message=message or "",
    )
    response = Response(
        status=ttrpc_status,
        payload=payload,
    )
    return response.SerializeToString()
