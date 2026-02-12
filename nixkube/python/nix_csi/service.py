import asyncio
import logging
import os
import socket

from asyncio import Semaphore
from collections import defaultdict
from csi import csi_grpc, csi_pb2
from functools import wraps
from grpclib import GRPCError
from grpclib.const import Status
from grpclib.server import Server, Stream
from kr8s.asyncio.objects import Pod
from pathlib import Path

from .builders import build_builder_args, get_builder_uris
from .cache import check_cache_connectivity, copy_to_cache, get_substituter_args
from .cleanup import cleanup_stale_entries, collect_active_volume_handles
from .constants import CSI_GCROOTS, CSI_SOCKET_PATH, CSI_VOLUMES, KUBE_POD_NAME, KUBE_POD_UID, NAMESPACE, NIX_BUILD_TIMEOUT
from .errors import CSIError
from .events import PodInfo, report_event
from .identityservicer import IdentityServicer
from .nix import build_flake_ref, build_nix_expr, build_store_path, get_current_system
from .store import extract_store_paths_set
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

            # Default to CSI pod info if not provided
            csi_pod_info = PodInfo(name=KUBE_POD_NAME, namespace=NAMESPACE, uid=KUBE_POD_UID)

            # Emit events for all exceptions
            if isinstance(e, CSIError):
                pod_info = e.pod_info if e.pod_info else csi_pod_info
                await report_event(
                    pod_info,
                    reason=e.reason,
                    note=e.message,
                    logs=e.logs,
                    event_type="Warning",
                )
            else:
                # Unexpected exception: emit CSI pod event
                await report_event(
                    csi_pod_info,
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
    volumeLocks: defaultdict[str, Semaphore] = defaultdict(Semaphore)

    def __init__(self, system: str):
        self.system = system

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

    async def _build_pod_packages(
        self,
        pod_info: PodInfo,
        gc_root: Path,
        extra_args: list[str],
    ) -> list[Path]:
        """Extract and build packages referenced in the pod spec."""
        pod = await Pod.get(pod_info.name, pod_info.namespace)
        if pod.metadata.uid != pod_info.uid:
            raise GRPCError(
                Status.INTERNAL, "poduid doesn't match", "poduid doesn't match"
            )

        package_paths = []
        pod_store_paths = extract_store_paths_set(pod.raw)

        for package_path in pod_store_paths:
            logger.debug(f"{package_path=}")
            async with self.volumeLocks[str(package_path)]:
                result_path = await build_store_path(
                    str(package_path),
                    gc_root,
                    extra_args,
                    timeout=NIX_BUILD_TIMEOUT,
                )
                package_paths.append(result_path)

        if package_paths:
            logger.debug(f"Extracted packages {package_paths=}")

        return package_paths

    async def _build_primary_package(
        self,
        store_path: str | None,
        flake_ref: str | None,
        nix_expr: str | None,
        gc_root: Path,
        extra_args: list[str],
    ) -> Path | None:
        """
        Build the primary package from various sources.

        Source selection order (intentional, documented in README):
        1. storePath - if present, use directly
        2. flakeRef - if storePath not present, build flake
        3. nixExpr - if neither above present, evaluate expression

        Users can specify multiple; first non-None in priority order is used.
        """
        if store_path is not None:
            async with self.volumeLocks[store_path]:
                logger.debug(f"{store_path=}")
                result = await build_store_path(
                    store_path,
                    gc_root,
                    extra_args,
                    timeout=NIX_BUILD_TIMEOUT,
                )
                return result

        if flake_ref is not None:
            async with self.volumeLocks[flake_ref]:
                logger.debug(f"{flake_ref=}")
                result = await build_flake_ref(
                    flake_ref,
                    gc_root,
                    extra_args,
                    timeout=NIX_BUILD_TIMEOUT,
                )
                return result

        if nix_expr is not None:
            async with self.volumeLocks[nix_expr]:
                logger.debug(f"{nix_expr=}")
                result = await build_nix_expr(
                    nix_expr,
                    gc_root,
                    extra_args,
                    timeout=NIX_BUILD_TIMEOUT,
                )
                return result

        return None

    @csi_error_handler
    async def NodePublishVolume(
        self,
        stream: Stream[
            csi_pb2.NodePublishVolumeRequest, csi_pb2.NodePublishVolumeResponse
        ],
    ) -> None:
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

        async with self.volumeLocks[request.volume_id]:
            gc_root = CSI_GCROOTS / request.volume_id
            volume_root = CSI_VOLUMES / request.volume_id
            extra_args = await self._get_build_args()

            # Extract pod info for event reporting
            pod_name = request.volume_context.get("csi.storage.k8s.io/pod.name")
            pod_namespace = request.volume_context.get("csi.storage.k8s.io/pod.namespace")
            pod_uid = request.volume_context.get("csi.storage.k8s.io/pod.uid")
            pod_info = PodInfo(name=pod_name, namespace=pod_namespace, uid=pod_uid)
            assert pod_info.name and pod_info.namespace and pod_info.uid, "Pod metadata missing from volume context"

            # Build packages from pod spec
            try:
                package_paths = await self._build_pod_packages(
                    pod_info,
                    gc_root,
                    extra_args,
                )
            except CSIError as e:
                e.pod_info = pod_info
                raise

            # Build primary package from volume attributes
            try:
                primary_package = await self._build_primary_package(
                    request.volume_context.get(self.system),
                    request.volume_context.get("flakeRef"),
                    request.volume_context.get("nixExpr"),
                    gc_root,
                    extra_args,
                )
            except CSIError as e:
                e.pod_info = pod_info
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
                # Report successful mount
                await report_event(
                    pod_info,
                    reason="VolumeMount",
                    note="Mounted Nix volume",
                    event_type="Normal",
                )
            except CSIError as e:
                cleanup_failed_volume(gc_root, volume_root)
                # Attach pod_info to exception for decorator to emit pod-specific event
                e.pod_info = pod_info
                raise
            except Exception as e:
                cleanup_failed_volume(gc_root, volume_root)
                raise

            await stream.send_message(csi_pb2.NodePublishVolumeResponse())

            # Copy to cache in background
            if primary_package is not None:
                task = asyncio.create_task(copy_to_cache(primary_package))
                task.add_done_callback(
                    lambda t: logger.error(f"copy_to_cache failed: {t.exception()}")
                    if t.exception()
                    else None
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
            raise GRPCError(Status.INVALID_ARGUMENT, "NodeUnpublishVolumeRequest is None")

        logger.info(
            "Unpublishing volume",
            extra={
                "volume_id": request.volume_id,
                "target_path": request.target_path,
            },
        )

        async with self.volumeLocks[request.volume_id]:
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
                    raise GRPCError(
                        Status.INTERNAL, f"removing {target_path=} failed", ex
                    )

            # Clean up stale gcroots and volume directories based on active volumes.
            # This catches orphaned resources from volumes that failed to unpublish cleanly.
            current_vol_data = target_path.parent / "vol_data.json"
            active_handles = collect_active_volume_handles(
                exclude_vol_data_path=current_vol_data
            )
            cleanup_stale_entries(active_handles)

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
            raise GRPCError(Status.INVALID_ARGUMENT, "NodeGetCapabilitiesRequest is None")
        await stream.send_message(csi_pb2.NodeGetCapabilitiesResponse(capabilities=[]))

    @csi_error_handler
    async def NodeGetInfo(
        self, stream: Stream[csi_pb2.NodeGetInfoRequest, csi_pb2.NodeGetInfoResponse]
    ) -> None:
        request = await stream.recv_message()
        if request is None:
            raise GRPCError(Status.INVALID_ARGUMENT, "NodeGetInfoRequest is None")
        node_name = os.environ.get("KUBE_NODE_NAME")
        if not node_name:
            raise GRPCError(
                Status.FAILED_PRECONDITION,
                "KUBE_NODE_NAME environment variable not set",
            )
        await stream.send_message(csi_pb2.NodeGetInfoResponse(node_id=node_name))

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

    identityServicer = IdentityServicer()
    try:
        system = await get_current_system()
    except CSIError as e:
        # Report event for CSI pod system detection failure
        csi_pod_info = PodInfo(name=KUBE_POD_NAME, namespace=NAMESPACE, uid=KUBE_POD_UID)
        await report_event(
            csi_pod_info,
            reason=e.reason,
            note=e.message,
            logs=e.logs,
            event_type="Warning",
        )
        raise
    nodeServicer = NodeServicer(system)

    server = Server(
        [
            identityServicer,
            nodeServicer,
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
