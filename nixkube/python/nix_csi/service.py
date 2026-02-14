# SPDX-License-Identifier: MIT

import asyncio
import logging
import socket
import time
from asyncio import Semaphore
from collections import defaultdict
from functools import wraps
from pathlib import Path

from csi import csi_grpc, csi_pb2
from grpclib import GRPCError
from grpclib.const import Status
from grpclib.server import Server, Stream
from kr8s.asyncio.objects import Pod

from .builders import build_builder_args, get_builder_uris
from .cache import check_cache_connectivity, copy_to_cache, get_substituter_args
from .cleanup import cleanup_stale_entries, collect_active_volume_handles
from .constants import (
    CSI_GCROOTS,
    CSI_SOCKET_PATH,
    CSI_VOLUMES,
    KUBE_NODE_NAME,
    KUBE_POD_NAME,
    NAMESPACE,
)
from .errors import CleanupStaleEntriesError, CSIError, RemoveVolumeDirError
from .events import report_event
from .identityservicer import IdentityServicer
from .nix import build_pod_packages, build_primary_package, get_current_system
from .volume import (
    cleanup_failed_volume,
    is_mount,
    mount_volume,
    prepare_volume,
    unmount,
)

logger = logging.getLogger("nix-csi")


def csi_error_handler(func):
    @wraps(func)
    async def wrapper(self, stream):
        try:
            return await func(self, stream)
        except Exception as e:
            logger.exception(f"{func.__name__} failed")

            # Emit events for all exceptions
            if isinstance(e, CSIError):
                # CSIError should already have pod set from the operation
                if e.pod:
                    await report_event(
                        e.pod,
                        reason=e.reason,
                        note=e.message,
                        logs=e.logs,
                        event_type="Warning",
                    )
            else:
                # Unexpected exception: emit CSI pod event (use pre-fetched pod from __init__)
                await report_event(
                    self.csi_pod,
                    reason="InternalError",
                    note=f"{func.__name__} failed: {type(e).__name__}",
                    logs=str(e),
                    event_type="Warning",
                )

            # Re-raise as GRPCError
            if isinstance(e, GRPCError):
                raise
            raise GRPCError(Status.INTERNAL, f"{type(e).__name__}: {e}")

    return wrapper


class NodeServicer(csi_grpc.NodeBase):
    volume_locks: defaultdict[str, Semaphore] = defaultdict(Semaphore)

    def __init__(self, system: str, csi_pod: Pod):
        self.system = system
        self.csi_pod = csi_pod

    async def _get_build_args(self) -> list[str]:
        """Get extra build arguments for builders and cache."""
        extra_args = []

        # Discover builder pods when builders are enabled
        # CSI pods run with --max-jobs 0 to delegate all builds to builder pods
        builder_uris = await get_builder_uris()
        if builder_uris:
            extra_args.extend(build_builder_args(builder_uris))
            logger.info(f"Using {len(builder_uris)} builder pods for builds")

        # Add cache as substituter if available
        if await check_cache_connectivity():
            extra_args.extend(get_substituter_args())

        return extra_args

    @csi_error_handler
    async def NodePublishVolume(
        self,
        stream: Stream[
            csi_pb2.NodePublishVolumeRequest, csi_pb2.NodePublishVolumeResponse
        ],
    ) -> None:
        start_time = time.perf_counter()
        request = await stream.recv_message()
        if request is None:
            raise GRPCError(Status.INVALID_ARGUMENT, "NodePublishVolumeRequest is None")

        logger.info(
            "Publishing volume",
            extra={
                "volume_id": request.volume_id,
                "target_path": request.target_path,
            },
        )

        if not request.volume_context.get("csi.storage.k8s.io/ephemeral"):
            raise GRPCError(
                Status.INVALID_ARGUMENT,
                "This CSI driver only supports ephemeral volumes",
            )

        async with self.volume_locks[request.volume_id]:
            gc_root = CSI_GCROOTS / request.volume_id
            volume_root = CSI_VOLUMES / request.volume_id
            extra_args = await self._get_build_args()

            # Fetch pod for event reporting and package extraction
            pod_name = request.volume_context["csi.storage.k8s.io/pod.name"]
            pod_namespace = request.volume_context["csi.storage.k8s.io/pod.namespace"]
            pod_uid = request.volume_context["csi.storage.k8s.io/pod.uid"]
            pod = await Pod.get(pod_name, namespace=pod_namespace)
            # Validate that fetched pod matches the UID from volume context
            assert pod.metadata.uid == pod_uid, (
                f"Pod UID mismatch: {pod.metadata.uid} != {pod_uid}"
            )

            # Build primary package from volume attributes
            try:
                store_path = request.volume_context.get(self.system)
                flake_ref = request.volume_context.get("flakeRef")
                nix_expr = request.volume_context.get("nixExpr")

                # Use first non-None value as lock key (same priority as build_primary_package)
                lock_key = store_path or flake_ref or nix_expr or "null"
                async with self.volume_locks[lock_key]:
                    primary_package = await build_primary_package(
                        store_path,
                        flake_ref,
                        nix_expr,
                        gc_root,
                        extra_args,
                    )
            except CSIError as e:
                e.pod = pod
                raise

            # Build packages from pod spec
            try:
                async with self.volume_locks[pod_uid]:
                    package_paths = await build_pod_packages(
                        pod,
                        gc_root,
                        extra_args,
                    )
            except CSIError as e:
                e.pod = pod
                raise

            if primary_package is not None:
                package_paths.append(primary_package)
                logger.debug(f"Primary package {primary_package=}")

            if not package_paths:
                logger.error("No packages to mount after building")
                raise GRPCError(
                    Status.INVALID_ARGUMENT,
                    "No packages to mount",
                )

            try:
                volume_root = await prepare_volume(
                    request.volume_id,
                    package_paths,
                    primary_package,
                )
                await mount_volume(
                    volume_root,
                    Path(request.target_path),
                    request.readonly,
                )
                # Report successful mount with closure size and elapsed time
                elapsed = time.perf_counter() - start_time
                await report_event(
                    pod,
                    reason="VolumeMount",
                    note=f"Mounted Nix volume with {len(package_paths)} store paths in {elapsed:.2f}s",
                    event_type="Normal",
                )
            except CSIError as e:
                cleanup_failed_volume(gc_root, volume_root)
                # Attach pod to exception for decorator to emit pod-specific event
                e.pod = pod
                raise
            except Exception:
                cleanup_failed_volume(gc_root, volume_root)
                raise

            await stream.send_message(csi_pb2.NodePublishVolumeResponse())

            # Copy all packages to cache in background
            if package_paths:
                task = asyncio.create_task(copy_to_cache(package_paths))
                task.add_done_callback(
                    lambda t: (
                        logger.error(f"copy_to_cache failed: {t.exception()}")
                        if t.exception()
                        else None
                    )
                )

    @csi_error_handler
    async def NodeUnpublishVolume(
        self,
        stream: Stream[
            csi_pb2.NodeUnpublishVolumeRequest, csi_pb2.NodeUnpublishVolumeResponse
        ],
    ) -> None:
        request = await stream.recv_message()
        if request is None:
            raise GRPCError(
                Status.INVALID_ARGUMENT, "NodeUnpublishVolumeRequest is None"
            )

        logger.info(
            "Unpublishing volume",
            extra={
                "volume_id": request.volume_id,
                "target_path": request.target_path,
            },
        )

        async with self.volume_locks[request.volume_id]:
            target_path = Path(request.target_path)

            # Cleanup operations are intentionally fail-fast (not wrapped in individual try/except).
            # Kubelet will retry NodeUnpublishVolume indefinitely on failure, so we want to
            # stop at the first error and let the retry start from scratch. This is safer than
            # attempting partial cleanup, as each retry re-attempts all steps in order.

            # Unmount
            if await is_mount(target_path):
                await unmount(target_path)
                logger.debug(f"unmounted {target_path=}")

            # Remove mount dir
            if target_path.exists():
                try:
                    target_path.rmdir()
                    logger.debug(f"removed {target_path=}")
                except Exception as ex:
                    raise RemoveVolumeDirError(
                        f"Failed to remove volume directory {target_path}",
                        logs=str(ex),
                    )

            # Clean up stale gcroots and volume directories based on active volumes.
            # This catches orphaned resources from volumes that failed to unpublish cleanly.
            # TODO: distinguish between failures cleaning our own volume vs other volumes
            try:
                current_vol_data = target_path.parent / "vol_data.json"
                active_handles = collect_active_volume_handles(
                    exclude_vol_data_path=current_vol_data
                )
                cleanup_stale_entries(active_handles)
            except Exception as ex:
                raise CleanupStaleEntriesError(
                    "Failed to cleanup stale volume entries",
                    logs=str(ex),
                )

            await stream.send_message(csi_pb2.NodeUnpublishVolumeResponse())

    @csi_error_handler
    async def NodeGetCapabilities(
        self,
        stream: Stream[
            csi_pb2.NodeGetCapabilitiesRequest, csi_pb2.NodeGetCapabilitiesResponse
        ],
    ) -> None:
        request = await stream.recv_message()
        if request is None:
            raise GRPCError(
                Status.INVALID_ARGUMENT, "NodeGetCapabilitiesRequest is None"
            )
        await stream.send_message(csi_pb2.NodeGetCapabilitiesResponse(capabilities=[]))

    @csi_error_handler
    async def NodeGetInfo(
        self, stream: Stream[csi_pb2.NodeGetInfoRequest, csi_pb2.NodeGetInfoResponse]
    ) -> None:
        request = await stream.recv_message()
        if request is None:
            raise GRPCError(Status.INVALID_ARGUMENT, "NodeGetInfoRequest is None")
        await stream.send_message(csi_pb2.NodeGetInfoResponse(node_id=KUBE_NODE_NAME))

    async def NodeGetVolumeStats(self, stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeGetVolumeStats not implemented")

    async def NodeExpandVolume(self, stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeExpandVolume not implemented")

    async def NodeStageVolume(self, stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeStageVolume not implemented")

    async def NodeUnstageVolume(self, stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeUnstageVolume not implemented")


async def serve():
    sock_path = CSI_SOCKET_PATH
    Path(sock_path).unlink(missing_ok=True)

    identity_servicer = IdentityServicer()
    # Fetch CSI pod and system once at startup, cache for entire service lifetime
    csi_pod = await Pod.get(KUBE_POD_NAME, namespace=NAMESPACE)
    try:
        system = await get_current_system()
    except CSIError as e:
        # Report event for CSI pod system detection failure
        await report_event(
            csi_pod,
            reason=e.reason,
            note=e.message,
            logs=e.logs,
            event_type="Warning",
        )
        raise
    node_servicer = NodeServicer(system, csi_pod)

    server = Server(
        [
            identity_servicer,
            node_servicer,
        ]
    )

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(sock_path)
        sock.listen(128)

        await server.start(sock=sock)
        logger.info(f"CSI driver (grpclib) listening on unix://{sock_path}")
        await server.wait_closed()
    except Exception:
        sock.close()
        Path(sock_path).unlink(missing_ok=True)
        raise
