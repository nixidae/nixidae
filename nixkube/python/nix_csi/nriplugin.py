# SPDX-License-Identifier: MIT
from nix_csi.volume import prepare_volume

import asyncio
import logging
import shutil
import struct
from pathlib import Path
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
from .hardlinks import hardlink_closure
from .nix import build_packages, get_build_args, get_closure_paths
from .store import extract_store_paths
from .zmq_server import ZeroMQServer

logger = logging.getLogger("nix-nri")

# Subscribe to all valid NRI events.
# Mirrors the Go formula: ValidEvents = (1 << (Event_LAST - 1)) - 1
# containerd rejects any events bits outside this mask.
_ALL_NRI_EVENTS = (1 << (api_pb2.Event.Value("LAST") - 1)) - 1


class NriPlugin(api_grpc.PluginBase):
    """NRI plugin with ZeroMQ build coordination."""

    def __init__(self, zmq_server: ZeroMQServer):
        super().__init__()
        self.zmq_server = zmq_server
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

        # Log container environment and args for debugging
        logger.debug(
            "[CreateContainer] Container args: %s",
            list(req.container.args) if req.container.args else [],
        )
        logger.debug(
            "[CreateContainer] Container env: %s",
            list(req.container.env) if req.container.env else [],
        )

        # Extract store paths from container env and args
        # Remove variable name prefix from env (keep only values)
        env_values = [
            env_var.split("=", 1)[1] for env_var in req.container.env if "=" in env_var
        ]
        # Combine env values and args
        combined = env_values + list(req.container.args)
        # Extract all store paths
        store_paths = extract_store_paths(combined)
        if store_paths:
            logger.info(
                f"[CreateContainer] Extracted store paths from container: {sorted(store_paths)}"
            )

        adjust = api_pb2.ContainerAdjustment()

        # Phase 1/2: Test NRI mount injection + build coordination (filter by nix-nri/test annotation)
        if "nix-nri/test" in req.pod.annotations:
            container_id = req.container.id
            # Pod-side path: /nix is /var/lib/nix-csi/nix mounted into the pod
            volume_path = Path(f"/nix/var/nix-csi/volumes/{container_id}")
            # Host-side path: what gets injected into user containers
            volume_path_host = Path(
                f"/var/lib/nix-csi/nix/var/nix-csi/volumes/{container_id}/nix"
            )

            logger.info(f"Creating volume dir for {container_id=} at {volume_path=}")

            try:
                # Create empty directory structure early (mount sources must exist at container creation time)
                (volume_path / "nix").mkdir(parents=True, exist_ok=True)
                logger.info(
                    "Created empty volume directory for backfill test at %r",
                    volume_path,
                )

                # Inject mount into container (using host-side path as source)
                mount = api_pb2.Mount(
                    destination="/nix",
                    source=str(volume_path_host),
                    type="bind",
                    options=["bind", "ro"],
                )
                adjust.mounts.append(mount)
                logger.info("Injected mount to /nix for container=%r", container_id)

                #  Inject OCI hook to wait for build completion
                assert self.nri_wait_bin is not None, (
                    "nri-wait binary not found on PATH, wait hook won't be able to execute"
                )
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

                # Phase 3: Spawn build task to build extracted store paths and backfill mounts
                if container_id not in self.zmq_server.pending_builds:
                    self.zmq_server.pending_builds.add(container_id)
                    logger.info(
                        "[CreateContainer] Spawning build task for container=%r with %d extracted store paths",
                        container_id,
                        len(store_paths),
                    )
                    # Spawn background task (fire and forget with exception logging)
                    task = asyncio.create_task(
                        self._spawn_build_task(container_id, store_paths, volume_path)
                    )
                    # Log task completion
                    task.add_done_callback(
                        lambda t: (
                            logger.info(
                                "[CreateContainer] Build task completed for container=%r",
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
        volume_path = Path(f"/nix/var/nix-csi/volumes/{container_id}")

        if volume_path.exists():
            try:
                shutil.rmtree(volume_path)
                logger.info("Cleaned up volume dir at %r", volume_path)
            except Exception as e:
                logger.warning(
                    "Failed to remove volume dir at %r: %s",
                    volume_path,
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

    async def _spawn_build_task(
        self, container_id: str, store_paths: set[Path], volume_path: Path
    ) -> None:
        """Realize, get closure, and hardlink store paths into the mount directory."""
        logger.info(
            "[BUILD-TASK] Started for container=%r with %d store paths",
            container_id,
            len(store_paths),
        )
        try:
            # If no store paths to build, just mark as done
            if not store_paths:
                logger.info(
                    "[BUILD-TASK] No store paths to build for container=%r",
                    container_id,
                )
                self.zmq_server.build_status[container_id] = {"status": "done"}
                await self.zmq_server.publish_build_complete(container_id)
                self.zmq_server.pending_builds.discard(container_id)
                return

            # Get extra build args for builders and cache
            extra_args = await get_build_args()

            # Realize storepaths
            await build_packages(
                store_paths, Path("/nix/var/nix-csi/volumes") / container_id, extra_args
            )
            # Get all paths
            paths = await get_closure_paths(store_paths)
            # Link all paths
            await prepare_volume(container_id, paths, None)

            logger.info(
                "[BUILD-TASK] Completed all phases for container=%r", container_id
            )
            self.zmq_server.build_status[container_id] = {"status": "done"}
            logger.debug(
                "[BUILD-TASK] Added to build_status cache for container=%r",
                container_id,
            )
            await self.zmq_server.publish_build_complete(container_id)
            self.zmq_server.pending_builds.discard(container_id)
            logger.info(
                "[BUILD-TASK] Removed from pending_builds for container=%r",
                container_id,
            )
        except Exception as e:
            logger.error("Build task failed for container=%r: %s", container_id, e)
            self.zmq_server.pending_builds.discard(container_id)


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

    # Initialize ZeroMQ server
    zmq_server = ZeroMQServer()
    await zmq_server.initialize()

    mapping: dict = {}
    plugin = NriPlugin(zmq_server)
    for h in [plugin]:
        mapping.update(h.__mapping__())

    handler = TtrpcHandler(mapping, codec)
    protocol = TtrpcProtocol(handler)
    protocol.connection_made(mux.channel_transport(PLUGIN_SERVICE_CONN))

    loop = asyncio.get_running_loop()
    read_task = loop.create_task(mux.read_loop())
    serve_task = loop.create_task(_serve_plugin_channel(mux, protocol))
    zmq_task = loop.create_task(zmq_server.start_request_handler())

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
        # Clean up ZeroMQ server
        zmq_server.shutdown()


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
