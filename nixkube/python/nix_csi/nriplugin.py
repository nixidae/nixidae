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
from grpclib_ttrpc.mux import PLUGIN_SERVICE_CONN, RUNTIME_SERVICE_CONN, NriMux
from grpclib_ttrpc.protocol import (
    HEADER_SIZE,
    MAX_PAYLOAD,
    MSG_TYPE_REQUEST,
    MSG_TYPE_RESPONSE,
    TtrpcProtocol,
)
from grpclib_ttrpc.server import TtrpcHandler
from nix_csi.volume import prepare_volume
from nri import api_grpc, api_pb2
from ttrpc.ttrpc_pb2 import Request, Response

from .constants import (
    HOST_MOUNT_PATH,
    NRI_PLUGIN_IDX,
    NRI_PLUGIN_NAME,
    NRI_RUNTIME_SOCKET,
)
from .hardlinks import deref_mount_hardlink_tree
from .nix import build_packages, get_build_args, get_closure_paths
from .ns_mount import mount_in_container
from .store import extract_store_paths
from .zmq_server import ZeroMQServer

logger = logging.getLogger("nix-nri")

# Subscribe to all valid NRI events.
# Mirrors the Go formula: ValidEvents = (1 << (Event_LAST - 1)) - 1
# containerd rejects any events bits outside this mask.
_ALL_NRI_EVENTS = (1 << (api_pb2.Event.Value("LAST") - 1)) - 1


def _parse_fhs_mounts_for_name(
    pod_annotations, target_name: str
) -> dict[Path, tuple[str, Path]]:
    """
    Parse FHS mount annotations matching a specific name (container name or "pod" for wildcard).

    Annotations format: nix-nri/{target-name}{-N}: [type:]/path/in/container=/nix/store/.../package
    Where N is optional suffix for multiple mounts, and type is "file" or "dir" (default: "dir").

    For wildcard (target_name="pod"):
      nix-nri/pod-1: dir:/etc/ssl/certs=/nix/store/cacert-1.0/etc/ssl/certs
      nix-nri/pod-2: file:/etc/passwd=/nix/store/fakeNss-1.0/etc/passwd

    For container (target_name="myapp"):
      nix-nri/myapp-1: file:/etc/ssl=/nix/store/cacert-2.0/etc/ssl

    Returns: {Path("/path/in/container"): ("file"|"dir", Path("/nix/store/.../package"))}
    """
    mounts: dict[Path, tuple[str, Path]] = {}
    prefix = f"nix-nri/{target_name}"

    for key, value in pod_annotations.items():
        # Match exact key or key with any suffix (nix-nri/{target-name} or nix-nri/{target-name}-{suffix})
        if key == prefix or key.startswith(prefix + "-"):
            # Parse optional type prefix: "file:..." or "dir:..." or bare "..."
            if value.startswith("file:"):
                mount_type = "file"
                rest = value[len("file:") :]
            elif value.startswith("dir:"):
                mount_type = "dir"
                rest = value[len("dir:") :]
            else:
                mount_type = "dir"
                rest = value

            if "=" in rest:
                container_path_str, store_path_str = rest.split("=", 1)
                mounts[Path(container_path_str)] = (mount_type, Path(store_path_str))

    return mounts


def parse_fhs_mounts(
    pod_annotations, container_name: str
) -> dict[Path, tuple[str, Path]]:
    """
    Parse FHS mount annotations from pod metadata.

    Supports two annotation patterns:
    1. Wildcard (apply to all containers):  nix-nri/pod: [type:]/etc/ssl=/nix/store/.../etc/ssl
    2. Container-specific (overrides wildcard): nix-nri/container-name: [type:]/etc/ssl=/nix/store/.../etc/ssl

    type is "file" or "dir" (default: "dir").

    Example annotations:
      nix-nri/pod: dir:/etc/ssl/certs=/nix/store/abc-cacert-1.0/etc/ssl/certs  (wildcard)
      nix-nri/pod: file:/etc/passwd=/nix/store/def-fakeNss/etc/passwd           (wildcard)
      nix-nri/myapp: file:/etc/passwd=/nix/store/ghi-fakeNss/etc/passwd         (container-specific)

    Returns dict: {Path("/path/in/container"): ("file"|"dir", Path("/nix/store/.../package"))}
    Container-specific annotations override wildcard mounts for the same path.
    """
    # Get wildcard mounts first
    wildcard_mounts = _parse_fhs_mounts_for_name(pod_annotations, "pod")

    # Get container-specific mounts (these override wildcards)
    container_mounts = _parse_fhs_mounts_for_name(pod_annotations, container_name)

    # Merge: container-specific overrides wildcard
    return {**wildcard_mounts, **container_mounts}


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

        # Combine env values, args and FHS mount annotation values for store path extraction
        # Only extract from nix-nri/pod or nix-nri/{container-name} annotations
        pod_prefix = "nix-nri/pod"
        container_prefix = f"nix-nri/{req.container.name}"
        fhs_annotation_values = [
            value
            for key, value in req.pod.annotations.items()
            if key == pod_prefix
            or key.startswith(pod_prefix + "-")
            or key == container_prefix
            or key.startswith(container_prefix + "-")
        ]
        combined = (
            list(req.container.env) + list(req.container.args) + fhs_annotation_values
        )
        # Extract all store paths
        store_paths = extract_store_paths(combined)
        if store_paths:
            logger.info(
                f"[CreateContainer] Extracted store paths from container: {sorted(store_paths)}"
            )

        # Parse FHS mount annotations (nix-nri/[container-name/]path)
        fhs_mounts = parse_fhs_mounts(req.pod.annotations, req.container.name)
        if fhs_mounts:
            logger.info(
                f"[CreateContainer] Parsed FHS mounts for container={req.container.name}: {fhs_mounts}"
            )

        adjust = api_pb2.ContainerAdjustment()

        # Check if /nix is already mounted (e.g., by nix-csi) to avoid collision
        nix_already_mounted = any(m.destination == "/nix" for m in req.container.mounts)

        # Enable NRI build if we have storepaths to inject and /nix isn't already mounted
        if store_paths and not nix_already_mounted:
            container_id = req.container.id
            # Pod-side path: /nix is /var/lib/nix-csi/nix mounted into the pod
            volume_path = Path(f"/nix/var/nix-csi/volumes/{container_id}")
            # Host-side path: what gets injected into user containers
            volume_path_host = Path(
                f"{HOST_MOUNT_PATH}/nix/var/nix-csi/volumes/{container_id}/nix"
            )

            logger.info(
                "Enabling store injection for container=%r with %d storepaths",
                container_id,
                len(store_paths),
            )

            try:
                # Create empty directory structure early (mount sources must exist at container creation time)
                (volume_path / "nix").mkdir(parents=True, exist_ok=True)
                logger.info(
                    "Created empty volume directory at %r",
                    volume_path,
                )

                # Create FHS mount directories and inject mounts
                fhs_base = (
                    Path(HOST_MOUNT_PATH) / "nix/var/nix-csi/volumes" / container_id
                )
                for container_path, (mount_type, _) in fhs_mounts.items():
                    # container_path is Path("/etc/ssl") or Path("/lib64")
                    # Reparent absolute path to volume_path
                    relative_path = container_path.relative_to("/")
                    fhs_volume_dir = volume_path / relative_path
                    if mount_type == "file":
                        fhs_volume_dir.parent.mkdir(parents=True, exist_ok=True)
                        fhs_volume_dir.touch()
                    else:
                        fhs_volume_dir.mkdir(parents=True, exist_ok=True)
                    logger.debug(
                        f"Created FHS mount {mount_type} placeholder for {container_path} at {fhs_volume_dir}"
                    )

                    # Create mount pointing to host-side FHS directory
                    fhs_host_path = fhs_base / relative_path
                    mount = api_pb2.Mount(
                        destination=str(container_path),
                        source=str(fhs_host_path),
                        type="bind",
                        options=["bind", "ro"],
                    )
                    adjust.mounts.append(mount)
                    logger.info(
                        f"Injected FHS mount {container_path} for container={container_id}"
                    )

                # Inject primary /nix mount (using host-side path as source)
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
                    path="/usr/bin/env",  # This is POSIX
                    args=["chroot", HOST_MOUNT_PATH, self.nri_wait_bin],
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
                        self._spawn_build_task(
                            container_id, store_paths, volume_path, fhs_mounts
                        )
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

    async def _pump_build_progress(self, container_id: str) -> None:
        """Periodically publish build progress heartbeats to reset nri-wait timeout."""
        try:
            while True:
                await asyncio.sleep(10)
                await self.zmq_server.publish_build_progress(container_id)
        except asyncio.CancelledError:
            logger.debug(
                "[BUILD-PUMP] Progress pump cancelled for container=%r", container_id
            )

    async def _spawn_build_task(
        self,
        container_id: str,
        store_paths: set[Path],
        volume_path: Path,
        fhs_mounts: dict[Path, tuple[str, Path]] | None = None,
    ) -> None:
        """Realize, get closure, and hardlink store paths into the mount directory.

        Also backfills FHS mount directories if fhs_mounts is provided.
        Uses deref_mount_hardlink_tree to dereference the mount path symlink in FHS mounts.
        Periodically pumps progress updates to reset nri-wait timeout.
        """
        logger.info(
            "[BUILD-TASK] Started for container=%r with %d store paths",
            container_id,
            len(store_paths),
        )
        pump_task: Optional[asyncio.Task] = None
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

            # Start progress pump to keep nri-wait timeout reset during long builds
            pump_task = asyncio.create_task(self._pump_build_progress(container_id))
            logger.debug(
                "[BUILD-TASK] Started progress pump for container=%r", container_id
            )

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

            # Wait for nri-wait to report PID+bundle (arrives when the createRuntime hook starts).
            # Then bind-mount /nix → /nix2 inside the container namespace as a minimal test.
            container_info = await self.zmq_server.wait_for_pid(container_id)
            if container_info is not None:
                pid, bundle = container_info
                logger.info(
                    "[BUILD-TASK] Mounting /nix → /nix2 in container pid=%d bundle=%r",
                    pid,
                    bundle,
                )
                await mount_in_container(pid, bundle, [("/nix", "/nix2")])
            else:
                logger.warning(
                    "[BUILD-TASK] No PID/bundle received for container=%r, skipping ns mount",
                    container_id,
                )

            # Backfill FHS mounts with dereferenced store paths
            if fhs_mounts:
                for container_path, (_, store_path) in fhs_mounts.items():
                    fhs_volume_dir = volume_path / container_path.relative_to("/")
                    logger.info(
                        f"[BUILD-TASK] Backfilling FHS mount {container_path} from {store_path}"
                    )
                    # Dereference the store path symlink once, then hardlink the tree
                    deref_mount_hardlink_tree(store_path, fhs_volume_dir)

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
