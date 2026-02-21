# SPDX-License-Identifier: MIT

import asyncio
import logging
import struct
from typing import Optional

from grpclib.const import Status
from grpclib.encoding.proto import ProtoCodec
from grpclib.exceptions import GRPCError, ProtocolError
from grpclib_ttrpc.mux import PLUGIN_SERVICE_CONN, RUNTIME_SERVICE_CONN, NriMux
from grpclib_ttrpc.protocol import (
    HEADER_SIZE,
    MAX_PAYLOAD,
    MSG_TYPE_REQUEST,
    MSG_TYPE_RESPONSE,
    TtrpcProtocol,
)
from grpclib_ttrpc.server import TtrpcHandler
from nri import api_grpc, api_pb2
from ttrpc.ttrpc_pb2 import Request, Response

from .constants import NRI_PLUGIN_IDX, NRI_PLUGIN_NAME, NRI_RUNTIME_SOCKET

logger = logging.getLogger("nix-nri")

# Subscribe to all valid NRI events.
# Mirrors the Go formula: ValidEvents = (1 << (Event_LAST - 1)) - 1
# containerd rejects any events bits outside this mask.
_ALL_NRI_EVENTS = (1 << (api_pb2.Event.Value("LAST") - 1)) - 1


class NriPlugin(api_grpc.PluginBase):
    """Empty NRI plugin — logs every lifecycle event and passes through."""

    async def Configure(self, stream) -> None:
        req: api_pb2.ConfigureRequest | None = await stream.recv_message()
        logger.info(
            "Configure: runtime=%r version=%r",
            req.runtime_name if req else None,
            req.runtime_version if req else None,
        )
        await stream.send_message(api_pb2.ConfigureResponse(events=_ALL_NRI_EVENTS))

    async def Synchronize(self, stream) -> None:
        req: api_pb2.SynchronizeRequest | None = await stream.recv_message()
        assert req is not None
        logger.info(
            "Synchronize: %d pods, %d containers",
            len(req.pods),
            len(req.containers),
        )
        await stream.send_message(api_pb2.SynchronizeResponse())

    async def Shutdown(self, stream) -> None:
        await stream.recv_message()
        logger.info("Shutdown")
        await stream.send_message(api_pb2.Empty())

    async def CreateContainer(self, stream) -> None:
        req: api_pb2.CreateContainerRequest | None = await stream.recv_message()
        assert req is not None
        logger.info(
            "CreateContainer: pod=%r container=%r",
            req.pod.name,
            req.container.name,
        )
        await stream.send_message(api_pb2.CreateContainerResponse())

    async def UpdateContainer(self, stream) -> None:
        req: api_pb2.UpdateContainerRequest | None = await stream.recv_message()
        assert req is not None
        logger.info(
            "UpdateContainer: container=%r",
            req.container.name,
        )
        await stream.send_message(api_pb2.UpdateContainerResponse())

    async def StopContainer(self, stream) -> None:
        req: api_pb2.StopContainerRequest | None = await stream.recv_message()
        assert req is not None
        logger.info(
            "StopContainer: container=%r",
            req.container.name,
        )
        await stream.send_message(api_pb2.StopContainerResponse())

    async def UpdatePodSandbox(self, stream) -> None:
        req: api_pb2.UpdatePodSandboxRequest | None = await stream.recv_message()
        assert req is not None
        logger.info(
            "UpdatePodSandbox: pod=%r",
            req.pod.name,
        )
        await stream.send_message(api_pb2.UpdatePodSandboxResponse())

    async def StateChange(self, stream) -> None:
        event: api_pb2.StateChangeEvent | None = await stream.recv_message()
        assert event is not None
        logger.info(
            "StateChange: event=%r",
            event.event,
        )
        await stream.send_message(api_pb2.Empty())

    async def ValidateContainerAdjustment(self, stream) -> None:
        req: (
            api_pb2.ValidateContainerAdjustmentRequest | None
        ) = await stream.recv_message()
        assert req is not None
        logger.info(
            "ValidateContainerAdjustment: container=%r",
            req.container.name,
        )
        await stream.send_message(api_pb2.ValidateContainerAdjustmentResponse())


async def _serve_plugin_channel(
    mux: NriMux,
    protocol: TtrpcProtocol,
) -> None:
    """Feed mux chunks for ConnID=1 into the ttrpc protocol until EOF."""
    try:
        while True:
            chunk = await mux.read_channel(PLUGIN_SERVICE_CONN)
            if chunk is None:
                protocol.connection_lost(None)
                return
            protocol.data_received(chunk)
    except Exception as exc:
        protocol.connection_lost(exc)


async def _register_plugin(
    mux: NriMux,
    codec: ProtoCodec,
    *,
    timeout: float = 5.0,
) -> None:
    """Send RegisterPlugin on ConnID=2 and wait for the response.

    Wire format (ConnID=2 channel):
        mux header:   [conn_id=2: uint32 BE][length: uint32 BE]
        ttrpc header: [payload_len: uint32 BE][stream_id=1: uint32 BE]
                      [msg_type=REQUEST=0x01: uint8][flags=0x00: uint8]
        ttrpc payload: ttrpc.Request{service, method, payload, timeout_nano}
            where payload = RegisterPluginRequest{plugin_name, plugin_idx}
    """
    rpr = api_pb2.RegisterPluginRequest(
        plugin_name=NRI_PLUGIN_NAME,
        plugin_idx=NRI_PLUGIN_IDX,
    )
    inner_payload = codec.encode(rpr, api_pb2.RegisterPluginRequest)

    req = Request(
        service="nri.pkg.api.v1alpha1.Runtime",
        method="RegisterPlugin",
        payload=inner_payload,
        timeout_nano=int(timeout * 1e9),
    )
    req_bytes = req.SerializeToString()

    ttrpc_hdr = struct.pack(">IIBB", len(req_bytes), 1, MSG_TYPE_REQUEST, 0)
    ttrpc_frame = ttrpc_hdr + req_bytes
    mux_hdr = struct.pack(">II", RUNTIME_SERVICE_CONN, len(ttrpc_frame))

    logger.debug(
        "RegisterPlugin: sending %d-byte ttrpc frame on ConnID=%d",
        len(ttrpc_frame),
        RUNTIME_SERVICE_CONN,
    )
    mux.writer.write(mux_hdr + ttrpc_frame)
    await mux.writer.drain()

    # Accumulate mux chunks for ConnID=2 until we have a complete ttRPC frame.
    buf = bytearray()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise asyncio.TimeoutError("Timed out waiting for RegisterPlugin response")
        chunk: Optional[bytes] = await asyncio.wait_for(
            mux.read_channel(RUNTIME_SERVICE_CONN), timeout=remaining
        )
        if chunk is None:
            raise ProtocolError("Connection closed waiting for RegisterPlugin response")
        buf.extend(chunk)
        if len(buf) < HEADER_SIZE:
            continue
        payload_len, _stream_id, msg_type, _flags = struct.unpack_from(">IIBB", buf)
        if payload_len > MAX_PAYLOAD:
            raise ProtocolError(
                f"RegisterPlugin response payload too large: {payload_len}"
            )
        if len(buf) < HEADER_SIZE + payload_len:
            continue  # wait for more chunks
        if msg_type != MSG_TYPE_RESPONSE:
            raise ProtocolError(
                f"Expected RESPONSE (0x{MSG_TYPE_RESPONSE:02x}), got 0x{msg_type:02x}"
            )
        resp_bytes = bytes(buf[HEADER_SIZE : HEADER_SIZE + payload_len])
        resp = Response.FromString(resp_bytes)
        if resp.status.code != 0:
            raise GRPCError(Status(resp.status.code), resp.status.message or None)
        logger.debug("RegisterPlugin response: OK")
        return


async def _nri_run() -> None:
    """Connect to nri.sock, set up mux, register, then serve until disconnect."""
    logger.info(
        "Connecting to socket %s (plugin=%s idx=%s)",
        NRI_RUNTIME_SOCKET,
        NRI_PLUGIN_NAME,
        NRI_PLUGIN_IDX,
    )
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(NRI_RUNTIME_SOCKET), timeout=5.0
    )
    mux = NriMux(reader, writer)
    codec = ProtoCodec()

    mapping: dict = {}
    for h in [NriPlugin()]:
        mapping.update(h.__mapping__())

    handler = TtrpcHandler(mapping, codec)
    protocol = TtrpcProtocol(handler)
    protocol.connection_made(mux.channel_transport(PLUGIN_SERVICE_CONN))

    loop = asyncio.get_running_loop()
    read_task = loop.create_task(mux.read_loop())
    serve_task = loop.create_task(_serve_plugin_channel(mux, protocol))

    try:
        await _register_plugin(mux, codec)
        logger.info(
            "Plugin registered (name=%r idx=%r)", NRI_PLUGIN_NAME, NRI_PLUGIN_IDX
        )
        # Block until the connection drops (read_loop exits → serve_task exits).
        await asyncio.gather(read_task, serve_task)
    finally:
        read_task.cancel()
        serve_task.cancel()
        # Await cancelled tasks so they finish before we reconnect — prevents
        # a second connection racing with cleanup of the first.
        await asyncio.gather(read_task, serve_task, return_exceptions=True)
        handler.close()
        await handler.wait_closed()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass


async def nri_serve() -> None:
    """Run the NRI plugin, reconnecting on failure with exponential backoff."""
    delay = 1.0
    while True:
        try:
            await _nri_run()
        except Exception as e:
            logger.warning(
                "Connection failed (%s: %s), retrying in %.0fs",
                type(e).__name__,
                e,
                delay,
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        else:
            # Clean disconnect: brief pause so containerd processes the old
            # connection's close before we re-register the same plugin identity.
            await asyncio.sleep(1.0)
            delay = 1.0
