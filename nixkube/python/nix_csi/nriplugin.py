# SPDX-License-Identifier: MIT

import asyncio
import json
import logging
import shutil
import struct
from pathlib import Path
from typing import Optional

import zmq.asyncio
from cachetools import TTLCache

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
    """NRI plugin with ZeroMQ build coordination."""

    def __init__(self):
        super().__init__()
        self.zmq_context: Optional[zmq.asyncio.Context] = None
        self.rep_socket: Optional[zmq.asyncio.Socket] = None
        self.pub_socket: Optional[zmq.asyncio.Socket] = None
        # Build status cache: container_id -> {"status": "done"|"pending", "timestamp": float}
        self.build_status: TTLCache = TTLCache(maxsize=10000, ttl=3600)
        self.pending_builds: set[str] = set()  # container IDs currently being built
        # Find nri-wait binary on PATH (available as nix-csi dependency)
        self.nri_wait_bin = shutil.which("wait")
        logger.debug("nri-wait binary resolved to: %s", self.nri_wait_bin)

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

        adjust = api_pb2.ContainerAdjustment()

        # Phase 1/2: Test NRI mount injection + build coordination (filter by nix-nri/test annotation)
        if "nix-nri/test" in req.pod.annotations:
            container_id = req.container.id
            # Pod-side path: /nix is /var/lib/nix-csi/nix mounted into the pod
            volume_path_in_pod = f"/nix/var/volumes/{container_id}"
            # Host-side path: what gets injected into user containers
            volume_path_on_host = f"/var/lib/nix-csi/nix/var/volumes/{container_id}"

            logger.info(
                "Creating volume dir for container=%r at %r",
                container_id,
                volume_path_in_pod,
            )

            try:
                # Create directory structure (using pod-side path)
                volume_path = Path(volume_path_in_pod)
                volume_path.mkdir(parents=True, exist_ok=True)

                # Write test file to verify mount works
                test_file = volume_path / "test.txt"
                test_file.write_text(
                    f"NRI test file\n"
                    f"Pod: {req.pod.name}\n"
                    f"Container: {req.container.name}\n"
                    f"Container ID: {container_id}\n"
                )

                logger.info("Created test file at %r", test_file)

                # Inject mount into container (using host-side path as source)
                mount = api_pb2.Mount(
                    destination="/nix-test",
                    source=volume_path_on_host,
                    type="bind",
                    options=["bind", "ro"],
                )
                adjust.mounts.append(mount)
                logger.info("Injected mount for container=%r", container_id)

                # Phase 2: Inject OCI hook to wait for build completion
                assert self.nri_wait_bin is not None, "nri-wait binary not found on PATH"
                hook = api_pb2.Hook(
                    path="/usr/bin/env",
                    args=["chroot", "/var/lib/nix-csi", self.nri_wait_bin],
                    env=[
                        f"NRI_CONTAINER_ID={container_id}",
                        "NRI_QUERY_SOCKET=/nix/var/nix-csi/wait-req.sock",
                        "NRI_PUB_SOCKET=/nix/var/nix-csi/wait-pub.sock",
                        "NRI_TIMEOUT=30",
                    ],
                )
                adjust.hooks.create_runtime.append(hook)
                logger.info(
                    "[CreateContainer] Injected createRuntime hook for container=%r (binary=%r)",
                    container_id,
                    self.nri_wait_bin,
                )

                # Phase 2: Spawn LARP build task to test ZeroMQ communication
                if container_id not in self.pending_builds:
                    self.pending_builds.add(container_id)
                    logger.info(
                        "[CreateContainer] Spawning LARP build task for container=%r",
                        container_id,
                    )
                    # Spawn background task (fire and forget with exception logging)
                    task = asyncio.create_task(self._spawn_larp_build(container_id, delay=2.0))
                    # Log task completion
                    task.add_done_callback(
                        lambda t: (
                            logger.info(
                                "[CreateContainer] LARP build task completed for container=%r",
                                container_id,
                            )
                            if not t.cancelled()
                            else None
                        )
                    )
                else:
                    logger.warning(
                        "[CreateContainer] Build already pending for container=%r",
                        container_id,
                    )

            except Exception as e:
                logger.exception(
                    "Failed to set up volume for container=%r: %s",
                    container_id,
                    e,
                )

        resp = api_pb2.CreateContainerResponse(adjust=adjust)
        await stream.send_message(resp)

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

        # Phase 1: Cleanup volume directory if it was created
        container_id = req.container.id
        # Use pod-side path for cleanup (same as creation)
        volume_path_in_pod = f"/nix/var/volumes/{container_id}"
        volume_path = Path(volume_path_in_pod)

        if volume_path.exists():
            try:
                shutil.rmtree(volume_path)
                logger.info("Cleaned up volume dir at %r", volume_path_in_pod)
            except Exception as e:
                logger.warning(
                    "Failed to remove volume dir at %r: %s",
                    volume_path_in_pod,
                    e,
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

    async def _spawn_larp_build(self, container_id: str, delay: float = 2.0) -> None:
        """Simulate a build: sleep then publish completion (LARP for Phase 2 testing)."""
        logger.info(
            "[LARP-BUILD] Started for container=%r (will complete in %.1fs)",
            container_id,
            delay,
        )
        try:
            await asyncio.sleep(delay)
            logger.info("[LARP-BUILD] Simulated build completed for container=%r", container_id)
            self.build_status[container_id] = {"status": "done"}
            logger.debug("[LARP-BUILD] Added to build_status cache for container=%r", container_id)
            await self._publish_build_complete(container_id)
            self.pending_builds.discard(container_id)
            logger.info("[LARP-BUILD] Removed from pending_builds for container=%r", container_id)
        except Exception as e:
            logger.error("LARP build task failed for container=%r: %s", container_id, e)

    async def _publish_build_complete(self, container_id: str) -> None:
        """Publish build completion message on PUB socket."""
        if self.pub_socket is None:
            logger.warning("PUB socket not initialized, cannot publish")
            return
        try:
            msg = json.dumps({"container_id": container_id, "status": "done"})
            logger.debug("[ZMQ-PUB] Publishing: %s", msg)
            await self.pub_socket.send(msg.encode())
            logger.info("[ZMQ-PUB] Published build completion for container=%r", container_id)
        except Exception as e:
            logger.error(
                "[ZMQ-PUB] Failed to publish build completion for container=%r: %s", container_id, e
            )

    async def _query_build_status(self, container_id: str) -> dict:
        """Return build status for a container (used by REP socket handler)."""
        if container_id in self.build_status:
            return self.build_status[container_id]
        elif container_id in self.pending_builds:
            return {"status": "pending"}
        else:
            return {"status": "unknown"}


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


async def _handle_zmq_requests(plugin: NriPlugin) -> None:
    """Handle build status queries on REP socket."""
    if plugin.rep_socket is None:
        logger.warning("REP socket not initialized, cannot handle requests")
        return

    logger.info("Starting ZeroMQ REP socket handler")
    try:
        while True:
            logger.debug("Waiting for build status query on REP socket...")
            query_bytes = await plugin.rep_socket.recv()
            logger.debug("Received query: %d bytes", len(query_bytes))
            try:
                query = json.loads(query_bytes.decode())
                container_id = query.get("container_id")
                logger.info("[ZMQ-REP] Query for container=%r", container_id)

                status = await plugin._query_build_status(container_id)
                logger.debug("[ZMQ-REP] Responding with status=%s for container=%r", status, container_id)
                response = json.dumps(status)
                await plugin.rep_socket.send(response.encode())
                logger.debug("[ZMQ-REP] Response sent")
            except Exception as e:
                logger.error("Error handling query: %s", e)
                await plugin.rep_socket.send(b'{"error":"internal error"}')
    except asyncio.CancelledError:
        logger.info("REP socket handler cancelled")
    except Exception as e:
        logger.error("REP socket handler error: %s", e)


async def _init_zmq_sockets(plugin: NriPlugin, socket_base_dir: str = "/nix/var/nix-csi") -> None:
    """Initialize ZeroMQ sockets (REP and PUB)."""
    logger.info("[ZMQ-INIT] Initializing ZeroMQ sockets in %r", socket_base_dir)

    # Ensure socket directory exists
    socket_dir = Path(socket_base_dir)
    try:
        socket_dir.mkdir(parents=True, exist_ok=True)
        logger.debug("[ZMQ-INIT] Socket directory ready: %s", socket_dir)
    except Exception as e:
        logger.error("[ZMQ-INIT] Failed to create socket directory: %s", e)
        raise

    # Create ZeroMQ context
    try:
        plugin.zmq_context = zmq.asyncio.Context()
        logger.debug("[ZMQ-INIT] ZeroMQ context created")
    except Exception as e:
        logger.error("[ZMQ-INIT] Failed to create ZMQ context: %s", e)
        raise

    # Create REP socket (for queries)
    try:
        req_socket_path = socket_dir / "wait-req.sock"
        plugin.rep_socket = plugin.zmq_context.socket(zmq.REP)
        plugin.rep_socket.bind(f"ipc://{req_socket_path}")
        logger.info("[ZMQ-INIT] REP socket bound to ipc://%s", req_socket_path)
    except Exception as e:
        logger.error("[ZMQ-INIT] Failed to create REP socket: %s", e)
        raise

    # Create PUB socket (for broadcasts)
    try:
        pub_socket_path = socket_dir / "wait-pub.sock"
        plugin.pub_socket = plugin.zmq_context.socket(zmq.PUB)
        plugin.pub_socket.bind(f"ipc://{pub_socket_path}")
        logger.info("[ZMQ-INIT] PUB socket bound to ipc://%s", pub_socket_path)
    except Exception as e:
        logger.error("[ZMQ-INIT] Failed to create PUB socket: %s", e)
        raise

    logger.info("[ZMQ-INIT] Both ZeroMQ sockets initialized successfully")


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
    plugin = NriPlugin()
    for h in [plugin]:
        mapping.update(h.__mapping__())

    # Initialize ZeroMQ sockets and handler
    await _init_zmq_sockets(plugin)

    handler = TtrpcHandler(mapping, codec)
    protocol = TtrpcProtocol(handler)
    protocol.connection_made(mux.channel_transport(PLUGIN_SERVICE_CONN))

    loop = asyncio.get_running_loop()
    read_task = loop.create_task(mux.read_loop())
    serve_task = loop.create_task(_serve_plugin_channel(mux, protocol))
    zmq_task = loop.create_task(_handle_zmq_requests(plugin))

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
        zmq_task.cancel()
        # Await cancelled tasks so they finish before we reconnect — prevents
        # a second connection racing with cleanup of the first.
        await asyncio.gather(read_task, serve_task, zmq_task, return_exceptions=True)
        handler.close()
        await handler.wait_closed()
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
        # Clean up ZeroMQ context
        if plugin.zmq_context is not None:
            plugin.zmq_context.term()


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
