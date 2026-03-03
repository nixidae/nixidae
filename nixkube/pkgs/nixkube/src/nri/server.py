# SPDX-License-Identifier: MIT
import asyncio
import logging
import shutil
import struct
from pathlib import Path
from typing import Optional

from grpclib.const import Status
from grpclib.encoding.proto import ProtoCodec
from grpclib.exceptions import GRPCError, ProtocolError
from grpclib_nri import PLUGIN_SERVICE_CONN, RUNTIME_SERVICE_CONN, NriMux
from grpclib_ttrpc.protocol import (
    HEADER_SIZE,
    MAX_PAYLOAD,
    MSG_TYPE_REQUEST,
    MSG_TYPE_RESPONSE,
    TtrpcProtocol,
)
from grpclib_ttrpc.server import TtrpcHandler
from kr8s.asyncio.objects import Pod
from nri import nri_grpc, nri_pb2
from ttrpc.ttrpc_pb2 import Request, Response

from ..cache import copy_to_cache
from ..constants import (
    COREUTILS_STATIC,
    HOST_MOUNT_PATH,
    NRI_CONTAINERS,
    NRI_PLUGIN_IDX,
    NRI_PLUGIN_NAME,
    NRI_RUNTIME_SOCKET,
)
from ..cri import get_cri_socket
from ..events import report_event
from ..nix import build_packages, get_build_args, get_closure_paths, get_current_system
from ..store import extract_store_paths
from ..volume import prepare_volume
from .annotations import parse_nix_rw, parse_store_mounts
from .cleanup import cleanup_container_volume, garbage_collect_stale_volumes
from .ns_mount import mount_in_container
from .zmq_server import ZeroMQServer

# Subscribe to all valid NRI events (containerd may not send CreateContainer
# unless we also subscribe to pod events and other related event types)
_SUBSCRIBED_EVENTS = (1 << (nri_pb2.Event.Value("LAST") - 1)) - 1


class NriPlugin(nri_grpc.PluginBase):
    """NRI plugin with ZeroMQ build coordination."""

    def __init__(self, zmq_server: ZeroMQServer, cri_socket: Path):
        logger = logging.getLogger("nixkube.nri.init")
        super().__init__()
        self.zmq_server = zmq_server
        self.cri_socket = cri_socket
        # Find nri-wait binary on PATH (available as nix-csi dependency)
        self.nri_wait_bin = shutil.which("wait")
        logger.debug(f"nri-wait binary resolved to: {self.nri_wait_bin}")

    async def Configure(self, stream) -> None:
        logger = logging.getLogger("nixkube.nri.configure")
        req: nri_pb2.ConfigureRequest | None = await stream.recv_message()
        runtime_name = req.runtime_name if req else None
        runtime_version = req.runtime_version if req else None
        logger.info(f"runtime={runtime_name!r} version={runtime_version!r}")
        await stream.send_message(nri_pb2.ConfigureResponse(events=_SUBSCRIBED_EVENTS))

    async def Synchronize(self, stream) -> None:
        logger = logging.getLogger("nixkube.nri.synchronize")
        req: nri_pb2.SynchronizeRequest | None = await stream.recv_message()
        assert req is not None
        logger.info(f"pods={len(req.pods)} containers={len(req.containers)}")
        await stream.send_message(nri_pb2.SynchronizeResponse())

    async def Shutdown(self, stream) -> None:
        logger = logging.getLogger("nixkube.nri.shutdown")
        await stream.recv_message()
        logger.info("Shutdown")
        await stream.send_message(nri_pb2.Empty())

    async def CreateContainer(self, stream) -> None:
        logger = logging.getLogger("nixkube.nri.createcontainer")
        req: nri_pb2.CreateContainerRequest | None = await stream.recv_message()
        assert req is not None
        logger.info(f"pod={req.pod.name!r} container={req.container.name!r}")

        # Check if /nix is already mounted (e.g., by nix-csi) to avoid collision
        if any(m.destination == "/nix" for m in req.container.mounts):
            logger.debug("Container already has /nix mounted, skipping NRI injection")
            resp = nri_pb2.CreateContainerResponse(adjust=nri_pb2.ContainerAdjustment())
            await stream.send_message(resp)
            return

        # Log container environment and args for debugging
        args = list(req.container.args) if req.container.args else []
        logger.debug(f"Container args: {args}")
        env = list(req.container.env) if req.container.env else []
        logger.debug(f"Container env: {env}")

        # Combine env values, args and store mount annotation values for store path extraction
        # Only extract from nixkube/pod or nixkube/{container-name} annotations
        # Include system-specific variants (e.g., nixkube/pod@x86_64-linux)
        pod_prefix = "nixkube/pod"
        container_prefix = f"nixkube/{req.container.name}"
        store_annotation_values = [
            value
            for key, value in req.pod.annotations.items()
            if key == pod_prefix
            or key.startswith(pod_prefix + "-")
            or key.startswith(pod_prefix + "@")
            or key == container_prefix
            or key.startswith(container_prefix + "-")
            or key.startswith(container_prefix + "@")
        ]
        combined = (
            list(req.container.env) + list(req.container.args) + store_annotation_values
        )
        # Extract all store paths
        store_paths = extract_store_paths(combined)
        if store_paths:
            logger.info(f"Extracted store paths from container: {sorted(store_paths)}")

        # Parse store mount annotations (nixkube/[container-name/]path), filtered by system
        store_mounts = parse_store_mounts(
            req.pod.annotations, req.container.name, get_current_system()
        )
        if store_mounts:
            logger.info(
                f"Parsed store mounts for container={req.container.name}: {store_mounts}"
            )

        # Parse RW flag (nixkube/pod-rw or nixkube/{container-name}-rw), filtered by system
        nix_rw = parse_nix_rw(
            req.pod.annotations, req.container.name, get_current_system()
        )
        if nix_rw:
            logger.info(
                f"RW /nix overlayfs requested for container={req.container.name!r}"
            )

        adjust = nri_pb2.ContainerAdjustment()

        # Enable NRI build if we have storepaths to inject
        if store_paths:
            container_id = req.container.id

            logger.info(
                "Enabling store injection for container=%r with %d storepaths",
                container_id,
                len(store_paths),
            )

            try:
                # Create Pod object for event reporting
                pod = Pod(
                    {
                        "metadata": {
                            "name": req.pod.name,
                            "namespace": req.pod.namespace,
                            "uid": req.pod.uid,
                        },
                    },
                    namespace=req.pod.namespace,
                )

                # Inject OCI hook to wait for build completion and report PID+bundle
                assert self.nri_wait_bin is not None, (
                    "nri-wait binary not found on PATH, wait hook won't be able to execute"
                )
                coreutils_binary = (
                    HOST_MOUNT_PATH
                    / COREUTILS_STATIC.relative_to("/")
                    / "bin/coreutils"
                )
                hook = nri_pb2.Hook(
                    path=str(coreutils_binary),
                    args=[
                        "chroot",  # somehow this works in OCI hooks but not --coreutils-prog=chroot....
                        str(HOST_MOUNT_PATH),
                        self.nri_wait_bin,
                    ],
                    env=[
                        "NRI_QUERY_SOCKET=/nix/var/nixkube/wait-req.sock",
                        "NRI_PUB_SOCKET=/nix/var/nixkube/wait-pub.sock",
                        "NRI_TIMEOUT=30",
                    ],
                )
                adjust.hooks.create_runtime.append(hook)
                logger.info(
                    f"Injected createRuntime hook for container={container_id!r} (binary={self.nri_wait_bin!r}) (chroot binary={coreutils_binary!r})"
                )

                # Spawn build task to build store paths and namespace-mount them into the container
                if container_id not in self.zmq_server.pending_builds:
                    self.zmq_server.pending_builds.add(container_id)
                    logger.info(
                        f"Spawning build task for container={container_id!r} with {len(store_paths)} extracted store paths"
                    )
                    # Spawn background task (fire and forget with exception logging)
                    task = asyncio.create_task(
                        self._spawn_build_task(
                            container_id,
                            req.container.name,
                            pod,
                            store_paths,
                            store_mounts,
                            nix_rw,
                        )
                    )
                    # Log task completion
                    task.add_done_callback(
                        lambda t: (
                            logger.info(
                                f"Build task completed for container={container_id!r}"
                            )
                            if not t.cancelled()
                            else None
                        )
                    )
                else:
                    logger.warning(
                        f"Build already pending for container={container_id!r}"
                    )

            except Exception as e:
                logger.exception(
                    f"Failed to set up volume for container={container_id!r}: {e}"
                )

        resp = nri_pb2.CreateContainerResponse(adjust=adjust)
        await stream.send_message(resp)

    async def UpdateContainer(self, stream) -> None:
        logger = logging.getLogger("nixkube.nri.updatecontainer")
        req: nri_pb2.UpdateContainerRequest | None = await stream.recv_message()
        assert req is not None
        logger.info(f"container={req.container.name!r}")
        await stream.send_message(nri_pb2.UpdateContainerResponse())

    async def StopContainer(self, stream) -> None:
        logger = logging.getLogger("nixkube.nri.stopcontainer")
        req: nri_pb2.StopContainerRequest | None = await stream.recv_message()
        assert req is not None
        logger.info(f"container={req.container.name!r}")

        container_id = req.container.id

        # Phase 1: Cleanup volume directory for this container
        await cleanup_container_volume(container_id)

        # Phase 2: Garbage collect stale volumes from containers no longer in CRI
        await garbage_collect_stale_volumes(self.cri_socket)

        await stream.send_message(nri_pb2.StopContainerResponse())

    async def UpdatePodSandbox(self, stream) -> None:
        logger = logging.getLogger("nixkube.nri.updatepodsandbox")
        req: nri_pb2.UpdatePodSandboxRequest | None = await stream.recv_message()
        assert req is not None
        logger.info(f"pod={req.pod.name!r}")
        await stream.send_message(nri_pb2.UpdatePodSandboxResponse())

    async def StateChange(self, stream) -> None:
        logger = logging.getLogger("nixkube.nri.statechange")
        event: nri_pb2.StateChangeEvent | None = await stream.recv_message()
        assert event is not None
        logger.info(
            f"StateChange: event={nri_pb2.Event.Name(event.event)} ({event.event})"
        )
        await stream.send_message(nri_pb2.Empty())

    async def ValidateContainerAdjustment(self, stream) -> None:
        logger = logging.getLogger("nixkube.nri.validatecontaineradjustment")
        req: (
            nri_pb2.ValidateContainerAdjustmentRequest | None
        ) = await stream.recv_message()
        assert req is not None
        logger.info(f"container={req.container.name!r}")
        await stream.send_message(nri_pb2.ValidateContainerAdjustmentResponse())

    async def _pump_build_progress(self, container_id: str) -> None:
        """Periodically publish build progress heartbeats to reset nri-wait timeout."""
        logger = logging.getLogger("nixkube.nri.buildpump")
        try:
            while True:
                await asyncio.sleep(10)
                await self.zmq_server.publish_build_progress(container_id)
        except asyncio.CancelledError:
            logger.debug(f"Progress pump cancelled for container={container_id!r}")

    async def _spawn_build_task(
        self,
        container_id: str,
        container_name: str,
        pod: Pod,
        store_paths: set[Path],
        store_mounts: dict[Path, Path] | None = None,
        nix_rw: bool = False,
    ) -> None:
        """Realize store paths, link into the volume, then namespace-mount store mounts.

        Periodically pumps progress updates to reset nri-wait timeout.
        """
        logger = logging.getLogger("nixkube.nri.buildtask")
        logger.info(
            f"Started for container={container_id!r} with {len(store_paths)} store paths"
        )
        pump_task: Optional[asyncio.Task] = None
        try:
            # If no store paths to build, just mark as done
            if not store_paths:
                logger.info(f"No store paths to build for container={container_id!r}")
                self.zmq_server.build_status[container_id] = {"status": "done"}
                await self.zmq_server.publish_build_complete(container_id)
                self.zmq_server.pending_builds.discard(container_id)
                return

            # Start progress pump to keep nri-wait timeout reset during long builds
            pump_task = asyncio.create_task(self._pump_build_progress(container_id))
            logger.debug(f"Started progress pump for container={container_id!r}")

            # Get extra build args for builders and cache
            extra_args = await get_build_args()

            # Realize storepaths
            volume_path = NRI_CONTAINERS / container_id
            logger.debug(
                f"Calling build_packages for container={container_id!r} with {len(store_paths)} paths"
            )
            await build_packages(store_paths, volume_path, extra_args)
            logger.debug(f"build_packages completed for container={container_id!r}")

            # Get all paths
            paths = await get_closure_paths(store_paths)
            # Hardlink closure into volume
            await prepare_volume(volume_path, paths, None)
            nix_tree_path = volume_path / "nix"

            # Wait for nri-wait to report PID+bundle (arrives when the createRuntime hook fires).
            # We need the PID to enter the container's mount namespace and mount /nix + store mounts.
            logger.debug(
                f"Waiting for PID+bundle from nri-wait for container={container_id!r}"
            )
            container_info = await self.zmq_server.wait_for_pid(container_id)
            pid_info = container_info[0] if container_info else None
            logger.debug(
                f"Received PID+bundle for container={container_id!r}: pid={pid_info}"
            )
            if container_info is None:
                raise RuntimeError(
                    f"No PID/bundle received for container={container_id!r}, cannot mount /nix"
                )
            pid, bundle = container_info

            ns_mounts = []
            if store_mounts:
                for container_path, store_path in store_mounts.items():
                    resolved = store_path.resolve()
                    if not resolved.exists():
                        raise ValueError(
                            f"Invalid store path in annotation: {store_path!r} → {container_path!r} "
                            f"(resolved: {resolved!r} does not exist)"
                        )
                    ns_mounts.append((resolved, container_path))

            logger.info(
                f"Namespace-mounting /nix + {len(ns_mounts)} store mount(s) in container pid={pid} bundle={bundle!r}"
            )
            await mount_in_container(pid, bundle, nix_tree_path, ns_mounts, nix_rw)

            logger.info(f"Completed all phases for container={container_id!r}")
            self.zmq_server.build_status[container_id] = {"status": "done"}
            logger.debug(f"Added to build_status cache for container={container_id!r}")
            await self.zmq_server.publish_build_complete(container_id)
            self.zmq_server.pending_builds.discard(container_id)
            logger.info(f"Removed from pending_builds for container={container_id!r}")

            # Copy all packages to cache in background
            if paths:
                task = asyncio.create_task(copy_to_cache(paths))
                task.add_done_callback(
                    lambda t: (
                        logger.error(f"copy_to_cache failed: {t.exception()}")
                        if t.exception()
                        else None
                    )
                )

            # Report successful build
            await report_event(
                pod,
                reason="BuildSucceeded",
                note=f"Successfully built {len(store_paths)} store path(s)",
                event_type="Normal",
            )
        except Exception as e:
            logger.error(f"Build task failed for container={container_id!r}: {e}")
            self.zmq_server.pending_builds.discard(container_id)

            # Report failed build
            await report_event(
                pod,
                reason="BuildFailed",
                note=f"Failed to build store paths for container {container_name}",
                logs=str(e),
                event_type="Warning",
            )
        finally:
            # Cancel progress pump if it's still running
            if pump_task is not None:
                pump_task.cancel()
                try:
                    await pump_task
                except asyncio.CancelledError:
                    pass


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
    logger = logging.getLogger("nixkube.nri.registerplugin")
    rpr = nri_pb2.RegisterPluginRequest(
        plugin_name=NRI_PLUGIN_NAME,
        plugin_idx=NRI_PLUGIN_IDX,
    )
    inner_payload = codec.encode(rpr, nri_pb2.RegisterPluginRequest)

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
        f"Sending {len(ttrpc_frame)}-byte ttrpc frame on ConnID={RUNTIME_SERVICE_CONN}"
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
        logger.debug("Response: OK")
        return


async def _nri_run() -> None:
    """Connect to nri.sock, set up mux, register, then serve until disconnect."""
    logger = logging.getLogger("nixkube.nri.runtime")
    logger.info(
        f"Connecting to socket {NRI_RUNTIME_SOCKET} (plugin={NRI_PLUGIN_NAME} idx={NRI_PLUGIN_IDX})"
    )
    reader, writer = await asyncio.wait_for(
        asyncio.open_unix_connection(NRI_RUNTIME_SOCKET), timeout=5.0
    )
    mux = NriMux(reader, writer)
    codec = ProtoCodec()

    # Initialize ZeroMQ server
    zmq_server = ZeroMQServer()
    await zmq_server.initialize()

    # Discover CRI socket for garbage collection
    cri_socket = await get_cri_socket()

    mapping: dict = {}
    plugin = NriPlugin(zmq_server, cri_socket)
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
            f"Plugin registered (name={NRI_PLUGIN_NAME!r} idx={NRI_PLUGIN_IDX!r})"
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
    logger = logging.getLogger("nixkube.nri.serve")
    delay = 1.0
    while True:
        try:
            await _nri_run()
        except Exception as e:
            logger.warning(
                f"Connection failed ({type(e).__name__}: {e}), retrying in {delay:.0f}s"
            )
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)
        else:
            # Clean disconnect: brief pause so containerd processes the old
            # connection's close before we re-register the same plugin identity.
            await asyncio.sleep(1.0)
            delay = 1.0
