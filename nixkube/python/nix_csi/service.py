import asyncio
import logging
import os
import shutil
import socket
import math
import tempfile

from csi import csi_grpc, csi_pb2
from grpclib import GRPCError
from grpclib.const import Status
from grpclib.server import Server
from importlib import metadata
from pathlib import Path
from asyncio import Semaphore, sleep
from collections import defaultdict
from .identityservicer import IdentityServicer
from .copytocache import copyToCache
from .subprocessing import run_captured, run_console, try_captured, try_console

logger = logging.getLogger("nix-csi")

CSI_PLUGIN_NAME = "nix.csi.store"
CSI_VENDOR_VERSION = metadata.version("nix-csi")

# Exit code from mount command when target is already mounted
MOUNT_ALREADY_MOUNTED = 32

# Paths we base everything on.
# Remember that these are CSI pod paths not node paths.
NIX_ROOT = Path("/")
CSI_ROOT = NIX_ROOT / "nix/var/nix-csi"
CSI_VOLUMES = CSI_ROOT / "volumes"
CSI_GCROOTS = NIX_ROOT / "nix/var/nix/gcroots/nix-csi"

# TODO: Make RSYNC_CONCURRENCY configurable from kubenix modules (deployment config)
# rather than only via environment variable
RSYNC_CONCURRENCY = Semaphore(int(os.environ.get("RSYNC_CONCURRENCY", "1")))


async def get_current_system():
    return (
        await try_captured(
            "nix", "eval", "--raw", "--impure", "--expr", "builtins.currentSystem"
        )
    ).stdout


def initialize():
    logger.info("Initializing NodeServicer")
    # Create directories we operate in
    CSI_ROOT.mkdir(parents=True, exist_ok=True)
    CSI_VOLUMES.mkdir(parents=True, exist_ok=True)
    CSI_GCROOTS.mkdir(parents=True, exist_ok=True)


class NodeServicer(csi_grpc.NodeBase):
    volumeLocks: defaultdict[str, Semaphore] = defaultdict(Semaphore)

    def __init__(self, system: str):
        self.system = system

    async def NodePublishVolume(self, stream):
        request: csi_pb2.NodePublishVolumeRequest | None = await stream.recv_message()
        if request is None:
            raise ValueError("NodePublishVolumeRequest is None")

        logger.info(f"Publish {request.target_path}")

        async with self.volumeLocks[request.volume_id]:
            targetPath = Path(request.target_path)
            storePath = request.volume_context.get(self.system, None)
            flakeRef = request.volume_context.get("flakeRef", None)
            nixExpr = request.volume_context.get("nixExpr", None)

            # Using sentinel path instead of None to avoid Optional type and None checks everywhere.
            # The if/elif blocks below should set this to a real path; the exists() check at line ~157
            # will fail if none of the branches executed (sentinel path doesn't exist).
            packagePath: Path = Path("/nonexistent/path/that/should/never/exist")
            gcPath = CSI_GCROOTS / request.volume_id

            extraArgs = []
            try:
                # Simple string check is fine - value controlled by easykubenix (always "true" or "false")
                if os.environ.get("CACHE_ENABLED", "false") == "true":
                    # Try cache connectivity with retries
                    for attempt in range(3):
                        try:
                            await asyncio.wait_for(
                                try_console("ssh", "nix@nix-cache", "--", "true"), timeout=2.0
                            )
                            extraArgs = [
                                "--extra-substituters",
                                "ssh-ng://nix@nix-cache?trusted=1&priority=20",
                            ]
                            logger.debug("Cache connectivity check succeeded")
                            break
                        except (GRPCError, OSError, asyncio.TimeoutError) as e:
                            if attempt < 2:
                                logger.debug(f"Cache check attempt {attempt + 1}/3 failed, retrying: {e}")
                            else:
                                logger.warning(f"Cache unavailable after 3 attempts, building without cache")
            except (GRPCError, OSError, asyncio.TimeoutError) as e:
                logger.warning(f"Cache connectivity check failed: {e}")

            # Source selection order (intentional, documented in README):
            # 1. storePath - if present, use directly
            # 2. flakeRef - if storePath not present, build flake
            # 3. nixExpr - if neither above present, evaluate expression
            # Users can specify multiple; first non-None in priority order is used.
            if storePath is not None:
                async with self.volumeLocks[storePath]:
                    logger.debug(f"{storePath=}")
                    packagePath = Path(storePath)
                    if not packagePath.exists():
                        await try_console(
                            "nix",
                            "build",
                            *extraArgs,
                            "--out-link",
                            gcPath,
                            packagePath,
                        )
            elif flakeRef is not None:
                async with self.volumeLocks[flakeRef]:
                    logger.debug(f"{flakeRef=}")

                    # Fetch storePath from caches
                    result = await try_console(
                        "nix",
                        "build",
                        *extraArgs,
                        "--print-out-paths",
                        "--out-link",
                        gcPath,
                        flakeRef,
                    )
                    packagePath = Path(result.stdout.splitlines()[0])
            elif nixExpr is not None:
                async with self.volumeLocks[nixExpr]:
                    logger.debug(f"{nixExpr=}")
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".nix") as tmp:
                        tmp.write(nixExpr)
                        tmp.flush()

                        # Fetch storePath from caches
                        result = await try_console(
                            "nix",
                            "build",
                            *extraArgs,
                            "--print-out-paths",
                            "--out-link",
                            gcPath,
                            "--file",
                            tmp.name,
                        )
                        packagePath = Path(result.stdout.splitlines()[0])
            else:
                raise GRPCError(
                    Status.INVALID_ARGUMENT,
                    f"Volume doesn't have correct volumeAttributes for {self.system}",
                )

            if not packagePath.exists():
                raise GRPCError(
                    Status.INVALID_ARGUMENT,
                    "packagePath turned out invalid",
                )

            # Root directory for volume. Contains /nix, also contains "workdir" and
            # "upperdir" if we're doing overlayfs
            volumeRoot = CSI_VOLUMES / request.volume_id
            # Capitalized to emphasise they're Nix environment variables
            NIX_STATE_DIR = volumeRoot / "nix/var/nix"
            # Create NIX_STATE_DIR where database will be initialized
            NIX_STATE_DIR.mkdir(parents=True, exist_ok=True)

            # Get closure
            paths = (
                await try_captured(
                    "nix",
                    "path-info",
                    "--recursive",
                    packagePath,
                )
            ).stdout.splitlines()

            try:
                # This try block is essentially nix copy into a chroot store with
                # extra steps. (Hardlinking instead of dumbcopying)

                # Install CSI gcroots
                await try_captured("nix", "build", "--out-link", gcPath, packagePath)

                # Copy closure to substore, rsync saves a lot of implementation
                # headache here. --archive keeps all attributes, --hard-links
                # hardlinks everything hardlinkable.
                async with RSYNC_CONCURRENCY:
                    await try_captured(
                        "rsync",
                        "--one-file-system",
                        "--recursive",
                        "--links",
                        "--hard-links",
                        "--mkpath",
                        *paths,
                        volumeRoot / "nix/store",
                    )

                # Create Nix database
                # This is a bash script that runs nix-store --dump-db | NIX_STATE_DIR=something nix-store --load-db
                await try_captured(
                    "nix_init_db",
                    NIX_STATE_DIR,
                    *paths,
                )

                # install gcroots in container using chroot store this is
                # required because the auto roots created for /nix/var/result
                # will point to Narnia while this one points into store.
                await try_captured(
                    "nix",
                    "build",
                    "--store",
                    volumeRoot,
                    "--out-link",
                    NIX_STATE_DIR / "gcroots/result",
                    packagePath,
                )

                # install /nix/var/result in container using chroot store
                await try_captured(
                    "nix",
                    "build",
                    "--store",
                    volumeRoot,
                    "--out-link",
                    volumeRoot / "nix/var/result",
                    packagePath,
                )
            except Exception as ex:
                # Remove gcroots if we failed something else
                gcPath.unlink(missing_ok=True)
                # Remove what we were working on
                shutil.rmtree(volumeRoot, True)
                raise

            targetPath.mkdir(parents=True, exist_ok=True)
            mountCommand = []
            if request.readonly:
                # For readonly we use a bind mount, the benefit is that different
                # container stores using bindmounts will get the same inodes and
                # share page cache with others, reducing memory usage.
                mountCommand = [
                    "mount",
                    "--verbose",
                    "--bind",
                    "-o",
                    "ro",
                    volumeRoot / "nix",
                    targetPath,
                ]
            else:
                # For readwrite we use an overlayfs mount, the benefit here is that
                # it works as CoW even if the underlying filesystem doesn't support
                # it, reducing host storage usage.
                workdir = volumeRoot / "workdir"
                upperdir = volumeRoot / "upperdir"
                workdir.mkdir(parents=True, exist_ok=True)
                upperdir.mkdir(parents=True, exist_ok=True)
                mountCommand = [
                    "mount",
                    "--verbose",
                    "-t",
                    "overlay",
                    "overlay",
                    "-o",
                    f"rw,lowerdir={volumeRoot / 'nix'},upperdir={upperdir},workdir={workdir}",
                    targetPath,
                ]

            mount = await run_console(*mountCommand)
            if mount.returncode == MOUNT_ALREADY_MOUNTED:
                logger.debug(f"Mount target {targetPath} was already mounted")
            elif mount.returncode != 0:
                # Clean up resources on mount failure
                gcPath.unlink(missing_ok=True)
                shutil.rmtree(volumeRoot, ignore_errors=True)
                raise GRPCError(
                    Status.INTERNAL,
                    f"Failed to mount {mount.returncode=} {mount.stderr=}",
                )

            reply = csi_pb2.NodePublishVolumeResponse()
            await stream.send_message(reply)

            task = asyncio.create_task(copyToCache(packagePath))
            task.add_done_callback(
                lambda t: logger.error(f"copyToCache failed: {t.exception()}")
                if t.exception() else None
            )

    @staticmethod
    async def IsMount(path: Path):
        return (await run_captured("findmnt", "--mountpoint", path)).returncode == 0

    @staticmethod
    async def Unmount(path: Path):
        return await run_captured("umount", "--verbose", path)

    async def NodeUnpublishVolume(self, stream):
        request: csi_pb2.NodeUnpublishVolumeRequest | None = await stream.recv_message()
        if request is None:
            raise ValueError("NodeUnpublishVolumeRequest is None")

        logger.info(f"Unpublish {request.target_path}")

        async with self.volumeLocks[request.volume_id]:
            targetPath = Path(request.target_path)

            # Cleanup operations are intentionally fail-fast (not wrapped in individual try/except).
            # Kubelet will retry NodeUnpublishVolume indefinitely on failure, so we want to
            # stop at the first error and let the retry start from scratch. This is safer than
            # attempting partial cleanup, as each retry re-attempts all steps in order.

            # Unmount
            if await NodeServicer.IsMount(targetPath):
                umount = await NodeServicer.Unmount(targetPath)
                if umount.returncode != 0 and await NodeServicer.IsMount(targetPath):
                    raise GRPCError(
                        Status.INTERNAL, "unmount failed", f"{umount.combined=}"
                    )
                else:
                    logger.debug(f"unmounted {request.target_path=}")

            # Remove mount dir
            if targetPath.exists():
                try:
                    targetPath.rmdir()
                    logger.debug(f"removed {targetPath=}")
                except Exception as ex:
                    raise GRPCError(
                        Status.INTERNAL, f"removing {targetPath=} failed", ex
                    )

            # Remove gcroots
            gcPath = CSI_GCROOTS / request.volume_id
            if gcPath.exists():
                try:
                    gcPath.unlink()
                    logger.debug(f"unlinked {gcPath=}")
                except Exception as ex:
                    raise GRPCError(
                        Status.INTERNAL, f"unlinking {targetPath=} failed", ex
                    )

            # Remove hardlink farm
            volumePath = CSI_VOLUMES / request.volume_id
            if volumePath.exists():
                try:
                    shutil.rmtree(volumePath)
                    logger.debug(f"removed {volumePath=}")
                except Exception as ex:
                    raise GRPCError(
                        Status.INTERNAL, f"recursive removing {targetPath=} failed", ex
                    )

            reply = csi_pb2.NodeUnpublishVolumeResponse()
            await stream.send_message(reply)

    async def NodeGetCapabilities(self, stream):
        request: csi_pb2.NodeGetCapabilitiesRequest | None = await stream.recv_message()
        if request is None:
            raise ValueError("NodeGetCapabilitiesRequest is None")
        reply = csi_pb2.NodeGetCapabilitiesResponse(capabilities=[])
        await stream.send_message(reply)

    async def NodeGetInfo(self, stream):
        request: csi_pb2.NodeGetInfoRequest | None = await stream.recv_message()
        if request is None:
            raise ValueError("NodeGetInfoRequest is None")
        node_name = os.environ.get("KUBE_NODE_NAME")
        if not node_name:
            raise GRPCError(Status.FAILED_PRECONDITION, "KUBE_NODE_NAME environment variable not set")
        reply = csi_pb2.NodeGetInfoResponse(node_id=node_name)
        await stream.send_message(reply)

    async def NodeGetVolumeStats(self, _stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeGetVolumeStats not implemented")

    async def NodeExpandVolume(self, _stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeExpandVolume not implemented")

    async def NodeStageVolume(self, _stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeStageVolume not implemented")

    async def NodeUnstageVolume(self, _stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeUnstageVolume not implemented")


async def serve():
    sock_path = "/csi/csi.sock"
    Path(sock_path).unlink(missing_ok=True)

    identityServicer = IdentityServicer()
    nodeServicer = NodeServicer(await get_current_system())
    initialize()

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
        # setblocking(False) is redundant - async IO handles this

        await server.start(sock=sock)
        logger.info(f"CSI driver (grpclib) listening on unix://{sock_path}")
        await server.wait_closed()
    except Exception:
        sock.close()
        Path(sock_path).unlink(missing_ok=True)
        raise
