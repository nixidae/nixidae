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

from ..cache import copy_to_cache
from ..constants import (
    CSI_GCROOTS,
    CSI_SOCKET_PATH,
    CSI_VOLUMES,
    KUBE_NODE_NAME,
    KUBE_POD_NAME,
    NAMESPACE,
)
from ..errors import CSIError
from ..events import report_event
from ..nix import (
    build_pod_packages,
    build_primary_package,
    get_build_args,
    get_current_system,
)
from ..volume import (
    cleanup_failed_volume,
    is_mount,
    mount_volume,
    prepare_volume,
    unmount,
)
from .cleanup import cleanup_stale_entries, collect_active_volume_handles
from .identity import IdentityServicer

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
            extra_args = await get_build_args()

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
                package_paths.add(primary_package)
                logger.debug(f"Primary package {primary_package=}")

            if not package_paths:
                logger.error("No packages to mount after building")
                raise GRPCError(
                    Status.INVALID_ARGUMENT,
                    "No packages to mount",
                )

            try:
                await prepare_volume(
                    volume_root,
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

            # Unmount the volume. CSI driver is responsible for unmounting only,
            # not for removing the kubelet-managed mount directory (that's kubelet's job).
            if await is_mount(target_path):
                await unmount(target_path)
                logger.debug(f"unmounted {target_path=}")
            else:
                logger.debug(f"path not mounted, skipping unmount {target_path=}")

            # Clean up stale gcroots and volume directories based on active volumes.
            # This catches orphaned resources from volumes that failed to unpublish cleanly.
            # Note: we do this cleanup even if above steps fail, to ensure CSI driver
            # resources in /nix/var/nix-csi are always cleaned up. Kubelet will retry
            # NodeUnpublishVolume if needed, but we should not block cleanup of our own resources.
            try:
                current_vol_data = target_path.parent / "vol_data.json"
                active_handles = collect_active_volume_handles(
                    exclude_vol_data_path=current_vol_data
                )
                cleanup_stale_entries(active_handles)
            except Exception as ex:
                logger.error(
                    f"Failed to cleanup stale volume entries for {request.volume_id}: {ex}"
                )
                # Report error event on CSI driver controller since we don't have pod info here
                try:
                    controller_pod = Pod(
                        {
                            "metadata": {
                                "name": KUBE_POD_NAME,
                                "namespace": NAMESPACE,
                            },
                        },
                        namespace=NAMESPACE,
                    )
                    await report_event(
                        controller_pod,
                        reason="VolumeCleanupFailed",
                        note=f"Failed to cleanup stale entries for volume {request.volume_id}: {str(ex)[:100]}",
                        event_type="Warning",
                    )
                except Exception as report_ex:
                    logger.warning(f"Failed to report cleanup error event: {report_ex}")
                # Don't raise - CSI driver should still return success if unmount succeeded

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


async def csi_serve():
    sock_path = CSI_SOCKET_PATH
    Path(sock_path).unlink(missing_ok=True)

    identity_servicer = IdentityServicer()
    # Fetch CSI pod once at startup, cache for entire service lifetime
    csi_pod = await Pod.get(KUBE_POD_NAME, namespace=NAMESPACE)
    node_servicer = NodeServicer(get_current_system(), csi_pod)

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
