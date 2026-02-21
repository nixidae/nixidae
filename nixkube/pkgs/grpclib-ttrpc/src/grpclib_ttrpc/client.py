"""ttrpc client: unary_call() for making a single RPC call over a ttrpc socket."""

import asyncio
import logging
import struct
from typing import Optional, Type, TypeVar

from grpclib.const import Status
from grpclib.encoding.base import CodecBase
from grpclib.encoding.proto import ProtoCodec
from grpclib.exceptions import GRPCError, ProtocolError
from ttrpc.ttrpc_pb2 import Request, Response

from .protocol import HEADER_SIZE, MAX_PAYLOAD, MSG_TYPE_REQUEST, MSG_TYPE_RESPONSE

log = logging.getLogger(__name__)

_T = TypeVar("_T")


async def unary_call(
    path: str,
    service: str,
    method: str,
    request: object,
    request_type: Type,
    response_type: Type[_T],
    *,
    connect_timeout: float = 5.0,
    response_timeout: float = 30.0,
    codec: Optional[CodecBase] = None,
) -> _T:
    """Make a single unary ttrpc RPC call over a Unix socket.

    Connects to *path*, sends one Request frame with stream_id=1,
    reads one Response frame, then closes the connection.

    Per the ttrpc spec, unary Request frames use flags=0.
    FLAG_REMOTE_CLOSED (0x01) is only set on client-streaming requests.

    :param path: Unix socket path.
    :param service: Fully-qualified protobuf service name
                    (e.g. ``"nri.pkg.api.v1alpha1.Runtime"``).
    :param method: RPC method name (e.g. ``"RegisterPlugin"``).
    :param request: Request message instance.
    :param request_type: Protobuf message class used to encode *request*.
    :param response_type: Protobuf message class used to decode the reply.
    :param connect_timeout: Timeout in seconds for establishing the connection.
    :param response_timeout: Timeout in seconds for receiving the response.
                             For calls like RegisterPlugin that trigger
                             server-side back-connections (Configure/Synchronize),
                             this should be generous (default: 30s).
    :param codec: Codec to use; defaults to :class:`ProtoCodec`.
    :raises GRPCError: If the server returns a non-OK status.
    :raises ProtocolError: On unexpected framing.
    """
    if codec is None:
        codec = ProtoCodec()

    log.debug("ttrpc unary_call: connecting to %s for %s/%s", path, service, method)
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(path), timeout=connect_timeout
    )

    try:
        payload = codec.encode(request, request_type)

        req = Request(
            service=service,
            method=method,
            payload=payload,
            timeout_nano=int(response_timeout * 1e9),
        )
        req_bytes = req.SerializeToString()

        if len(req_bytes) > MAX_PAYLOAD:
            raise ProtocolError(
                f"Request payload too large: {len(req_bytes)} > {MAX_PAYLOAD}"
            )

        # Per ttRPC PROTOCOL.md: unary Request frames use flags=0.
        # FLAG_REMOTE_CLOSED (0x01) signals streaming (client done sending
        # but expects multiple server responses) — NOT for unary calls.
        header = struct.pack(">IIBB", len(req_bytes), 1, MSG_TYPE_REQUEST, 0)
        log.debug(
            "ttrpc unary_call: sending %d-byte request (header=%s payload_hex=%s)",
            len(req_bytes),
            header.hex(),
            req_bytes[:64].hex(),
        )
        writer.write(header + req_bytes)
        await writer.drain()

        # Read response frame header (10 bytes).
        # response_timeout must be long enough for the server to do any
        # back-connects (e.g. containerd RegisterPlugin → Configure/Synchronize).
        log.debug(
            "ttrpc unary_call: waiting for response (timeout=%.0fs)", response_timeout
        )
        raw_header = await asyncio.wait_for(
            reader.readexactly(HEADER_SIZE), timeout=response_timeout
        )
        length, _stream_id, msg_type, resp_flags = struct.unpack(">IIBB", raw_header)
        log.debug(
            "ttrpc unary_call: response header: length=%d stream_id=%d "
            "msg_type=0x%02x flags=0x%02x",
            length,
            _stream_id,
            msg_type,
            resp_flags,
        )

        if msg_type != MSG_TYPE_RESPONSE:
            raise ProtocolError(
                f"Expected RESPONSE frame (0x{MSG_TYPE_RESPONSE:02x}), "
                f"got 0x{msg_type:02x}"
            )

        if length > MAX_PAYLOAD:
            raise ProtocolError(f"Response payload too large: {length}")

        resp_bytes = await asyncio.wait_for(
            reader.readexactly(length), timeout=response_timeout
        )
        log.debug(
            "ttrpc unary_call: response payload (%d bytes): %s",
            length,
            resp_bytes[:64].hex(),
        )
        resp = Response.FromString(resp_bytes)

        if resp.status.code != 0:
            log.debug(
                "ttrpc unary_call: RPC error code=%d message=%r",
                resp.status.code,
                resp.status.message,
            )
            raise GRPCError(
                Status(resp.status.code),
                resp.status.message or None,
            )

        log.debug("ttrpc unary_call: success")
        return codec.decode(resp.payload, response_type)

    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception as exc:
            log.debug("Error closing connection: %r", exc)
