import asyncio
import kr8s
import logging
import os
import re
import shutil
import socket
import tempfile

from .copytocache import copyToCache
from .identityservicer import IdentityServicer
from .subprocessing import run_captured, run_console, try_captured, try_console
from asyncio import Semaphore
from collections import defaultdict
from collections.abc import Mapping, Sequence
from csi import csi_grpc, csi_pb2
from grpclib import GRPCError
from grpclib.const import Status
from grpclib.server import Server
from functools import wraps
from importlib import metadata
from kr8s.asyncio.objects import Pod
from pathlib import Path
from typing import Any, Iterator

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

# Configurable via kubenix option: rsyncConcurrency (default: 1)
# Set via RSYNC_CONCURRENCY environment variable
RSYNC_CONCURRENCY = Semaphore(max(int(os.environ.get("RSYNC_CONCURRENCY", "1")), 1))

# Configurable via kubenix option: nodeBuildTimeout (default: 300)
# Set via NIX_BUILD_TIMEOUT environment variable
NIX_BUILD_TIMEOUT = float(os.environ.get("NIX_BUILD_TIMEOUT", "300"))

# Builder configuration
# Set via environment variables from kubenix when builders are enabled
BUILDERS_ENABLED = os.environ.get("BUILDERS_ENABLED", "false").lower() == "true"

NAMESPACE = os.environ.get("KUBE_NAMESPACE", "nix-csi")
BUILDERS_SERVICE = "nix-csi-builders"

# Nix base32 excludes: e, o, t, u
STORE_PATH_RE = re.compile(r"/?nix/store/([0-9a-df-np-sv-z]{32}-[^\s/]+)")


def extract_store_paths(value: Any) -> Iterator[Path]:
    match value:
        case str():
            for match in STORE_PATH_RE.findall(value):
                yield Path("/nix/store") / match
        case Mapping():
            for v in value.values():
                yield from extract_store_paths(v)
        case Sequence():
            for item in value:
                yield from extract_store_paths(item)


def extract_store_name(value: Path | str) -> str:
    return str(value).removeprefix("/nix/store/")


async def get_current_system():
    """Get system string evaluated by nix"""
    return (
        await try_captured(
            "nix", "eval", "--raw", "--impure", "--expr", "builtins.currentSystem"
        )
    ).stdout

def hardlink_tree(src: Path, dst: Path) -> None:
    """Hardlink a single directory tree, preserving symlinks."""
    dst.mkdir(parents=True, exist_ok=True)

    for entry in os.scandir(src):
        dst_path = dst / entry.name

        if entry.is_symlink():
            os.symlink(os.readlink(entry.path), dst_path)
        elif entry.is_dir(follow_symlinks=False):
            hardlink_tree(Path(entry.path), dst_path)
        elif entry.is_file(follow_symlinks=False):
            dst_path.hardlink_to(entry.path)


def hardlink_closure(store_paths: list[Path], dst: Path) -> None:
    """
    Hardlink multiple store paths into dst.

    store_paths: [/nix/store/abc-foo, /nix/store/def-bar, ...]
    dst: volume_root/nix/store
    result: dst/abc-foo/..., dst/def-bar/...
    """
    dst.mkdir(parents=True, exist_ok=True)

    for store_path in store_paths:
        target = dst / store_path.name
        if target.exists():
            continue  # already copied (deduplication across volumes)
        hardlink_tree(store_path, target)

def deref_hardlink_tree(src: Path, dst: Path) -> None:
    """
    Recursively copy src to dst, dereferencing symlinks and
    hardlinking files for space efficiency.

    All symlink targets must exist on the same filesystem as dst.
    """
    src = Path(src)
    dst = Path(dst)
    dst.mkdir(parents=True, exist_ok=True)

    for entry in os.scandir(src):
        dst_path = dst / entry.name
        resolved = Path(entry.path).resolve()

        if not resolved.exists():
            raise FileNotFoundError(f"Broken symlink: {entry.path} -> {resolved}")

        if resolved.is_dir():
            deref_hardlink_tree(resolved, dst_path)
        elif resolved.is_file():
            dst_path.hardlink_to(resolved)


async def get_builder_uris():
    """Query k8s API for builder pods, return list of SSH URIs for --builders flag."""
    if not BUILDERS_ENABLED:
        return []

    try:
        # Use kr8s to query pods with label selector
        # kr8s.asyncio.get() returns an async generator, iterate with async for
        uris = []
        async for pod in kr8s.asyncio.get(
            "pods", namespace=NAMESPACE, label_selector="app.kubernetes.io/name=builder"
        ):
            if pod.status.phase == "Running":
                pod_name = pod.metadata.name
                uri = f"ssh://nix@{pod_name}.{BUILDERS_SERVICE}.{NAMESPACE}.svc.cluster.local"
                uris.append(uri)

        logger.debug(f"Discovered {len(uris)} builder pods: {uris}")
        return uris
    except Exception as e:
        logger.warning(f"Failed to discover builder pods: {e}")
        return []


def csi_error_handler(func):
    @wraps(func)
    async def wrapper(self, stream):
        try:
            return await func(self, stream)
        except GRPCError:
            raise
        except Exception as e:
            logger.exception(f"{func.__name__} failed")
            raise GRPCError(Status.INTERNAL, f"{type(e).__name__}: {e}")

    return wrapper


class NodeServicer(csi_grpc.NodeBase):
    volumeLocks: defaultdict[str, Semaphore] = defaultdict(Semaphore)

    def __init__(self, system: str):
        self.system = system

    @csi_error_handler
    async def NodePublishVolume(self, stream):
        request: csi_pb2.NodePublishVolumeRequest | None = await stream.recv_message()
        if request is None:
            raise GRPCError(Status.INVALID_ARGUMENT, "NodePublishVolumeRequest is None")

        logger.info(f"Publish {request.target_path}")

        if not request.volume_context.get("csi.storage.k8s.io/ephemeral"):
            raise GRPCError(
                Status.INVALID_ARGUMENT,
                "This CSI driver only supports ephemeral volumes",
            )

        async with self.volumeLocks[request.volume_id]:
            target_path = Path(request.target_path)
            store_path = request.volume_context.get(self.system)
            flake_ref = request.volume_context.get("flakeRef")
            nix_expr = request.volume_context.get("nixExpr")

            # Using sentinel path instead of None to avoid Optional type and None checks everywhere.
            # The if/elif blocks below should set this to a real path; the exists() check at line ~157
            # will fail if none of the branches executed (sentinel path doesn't exist).
            primary_package_path = Path("/nonexistent/path/that/should/never/exist")
            gc_root = CSI_GCROOTS / request.volume_id

            extra_args = []

            # Discover builder pods when builders are enabled
            # CSI pods run with --max-jobs 0 to delegate all builds to builder pods
            builder_uris = await get_builder_uris()
            if builder_uris:
                extra_args.extend(["--max-jobs", "0"])
                for uri in builder_uris:
                    extra_args.extend(["--builders", uri])
                extra_args.append("--builders-use-substitutes")
                logger.info(f"Using {len(builder_uris)} builder pods for builds")

            # Simple string check is fine - value controlled by easykubenix (always "true" or "false")
            if os.environ.get("CACHE_ENABLED", "false") == "true":
                try:
                    logger.debug("Trying to connect to cache")
                    await asyncio.wait_for(
                        try_console("ssh", "nix@nix-cache", "--", "true"),
                        timeout=2.0,
                    )
                    extra_args.extend(
                        [
                            "--extra-substituters",
                            "ssh-ng://nix@nix-cache?trusted=1&priority=20",
                        ]
                    )
                    logger.debug("Cache connectivity check succeeded")
                except (GRPCError, OSError, asyncio.TimeoutError):
                    logger.warning("Cache connectivity check failed")

            package_paths = []

            pod_name = request.volume_context.get("csi.storage.k8s.io/pod.name")
            pod_ns = request.volume_context.get("csi.storage.k8s.io/pod.namespace")
            pod_uid = request.volume_context.get("csi.storage.k8s.io/pod.uid")

            if pod_name and pod_ns and pod_uid:
                pod = await Pod.get(pod_name, pod_ns)
                if pod.metadata.uid != pod_uid:
                    raise GRPCError(
                        Status.INTERNAL, "poduid doesn't match", "poduid doesn't match"
                    )

                pod = set(extract_store_paths(pod.raw))
                for package_path in pod:
                    logger.debug(f"{package_path=}")
                    async with self.volumeLocks[str(package_path)]:
                        name = extract_store_name(package_path)
                        logger.debug(f"{name=}")
                        result = await try_console(
                            "nix",
                            "build",
                            *extra_args,
                            "--print-out-paths",
                            "--out-link",
                            gc_root / name,
                            package_path,
                            timeout=NIX_BUILD_TIMEOUT,
                        )
                        package_paths.append(Path(result.stdout.splitlines()[0]))

            if package_paths:
                logger.debug(f"Extracted packages {package_paths=}")

            # Source selection order (intentional, documented in README):
            # 1. storePath - if present, use directly
            # 2. flakeRef - if storePath not present, build flake
            # 3. nixExpr - if neither above present, evaluate expression
            # Users can specify multiple; first non-None in priority order is used.
            if store_path is not None:
                async with self.volumeLocks[store_path]:
                    logger.debug(f"{store_path=}")
                    name = extract_store_name(store_path)
                    result = await try_console(
                        "nix",
                        "build",
                        *extra_args,
                        "--print-out-paths",
                        "--out-link",
                        gc_root / name,
                        store_path,
                        timeout=NIX_BUILD_TIMEOUT,
                    )
                    package_paths.append(Path(result.stdout.splitlines()[0]))
                    if not primary_package_path.exists():
                        primary_package_path = Path(result.stdout.splitlines()[0])

            if flake_ref is not None:
                async with self.volumeLocks[flake_ref]:
                    logger.debug(f"{flake_ref=}")

                    # Fetch storePath from caches
                    result = await try_console(
                        "nix",
                        "build",
                        *extra_args,
                        "--print-out-paths",
                        "--out-link",
                        gc_root / "flake",
                        flake_ref,
                        timeout=NIX_BUILD_TIMEOUT,
                    )
                    package_paths.append(Path(result.stdout.splitlines()[0]))
                    if not primary_package_path.exists():
                        primary_package_path = Path(result.stdout.splitlines()[0])

            if nix_expr is not None:
                async with self.volumeLocks[nix_expr]:
                    logger.debug(f"{nix_expr=}")
                    with tempfile.NamedTemporaryFile(mode="w", suffix=".nix") as tmp:
                        tmp.write(nix_expr)
                        tmp.flush()

                        # Fetch storePath from caches
                        result = await try_console(
                            "nix",
                            "build",
                            *extra_args,
                            "--print-out-paths",
                            "--out-link",
                            gc_root / "expr",
                            "--file",
                            tmp.name,
                            timeout=NIX_BUILD_TIMEOUT,
                        )
                        package_paths.append(Path(result.stdout.splitlines()[0]))
                        if not primary_package_path.exists():
                            primary_package_path = Path(result.stdout.splitlines()[0])

            if not primary_package_path.exists() and not package_paths:
                logger.error("packagePath doesn't exist after building")
                raise GRPCError(
                    Status.INVALID_ARGUMENT,
                    "packagePath turned out invalid",
                )
            elif primary_package_path.exists():
                logger.debug(f"Primary package {primary_package_path=}")

            # Root directory for volume. Contains /nix, also contains "workdir" and
            # "upperdir" if we're doing overlayfs
            volume_root = CSI_VOLUMES / request.volume_id
            # Capitalized to emphasise they're Nix environment variables
            NIX_STATE_DIR = volume_root / "nix/var/nix"
            # Create NIX_STATE_DIR where database will be initialized
            NIX_STATE_DIR.mkdir(parents=True, exist_ok=True)

            # Get storepaths from all packages
            store_paths = (
                await try_captured(
                    "nix",
                    "path-info",
                    "--recursive",
                    *package_paths,
                )
            ).stdout.splitlines()

            try:
                # This try block is essentially nix copy into a chroot store with
                # extra steps. (Hardlinking instead of dumbcopying)

                # Install CSI gcroots
                for package_path in package_paths:
                    name = extract_store_name(package_path)
                    await try_captured(
                        "nix",
                        "build",
                        "--out-link",
                        gc_root / name,
                        package_path,
                        timeout=NIX_BUILD_TIMEOUT,
                    )

                # Copy closure to substore
                hardlink_closure([Path(p) for p in store_paths], volume_root / "nix/store")

                # Create Nix database
                # This is a bash script that runs nix-store --dump-db | NIX_STATE_DIR=something nix-store --load-db
                await try_captured(
                    "nix_init_db",
                    NIX_STATE_DIR,
                    *store_paths,
                )

                # install gcroots in container using chroot, store this is
                # required because the auto roots created for /nix/var/result
                # will point to Narnia while this one points into store.
                for package_path in package_paths:
                    name = extract_store_name(package_path)
                    await try_captured(
                        "nix",
                        "build",
                        "--store",
                        volume_root,
                        "--out-link",
                        NIX_STATE_DIR / f"gcroots/{name}",
                        package_path,
                    )

                # install /nix/var/result in container using chroot store
                if primary_package_path.exists():
                    await try_captured(
                        "nix",
                        "build",
                        "--store",
                        volume_root,
                        "--out-link",
                        volume_root / "nix/var/result",
                        primary_package_path,
                    )

                    # Create hardlink farm of primary package to volume_root
                    await asyncio.to_thread(deref_hardlink_tree, primary_package_path, volume_root)
                else:
                    logger.debug(f"{primary_package_path=} doesn't exist")

            except Exception:
                logger.exception("Failed to build volume")
                # Remove gcroots if we failed something else
                shutil.rmtree(gc_root, ignore_errors=True)
                # Remove what we were working on
                shutil.rmtree(volume_root, True)
                raise

            target_path.mkdir(parents=True, exist_ok=True)
            mount_command = []
            if request.readonly:
                # For readonly we use a bind mount, the benefit is that different
                # container stores using bindmounts will get the same inodes and
                # share page cache with others, reducing memory usage.
                mount_command = [
                    "mount",
                    "--verbose",
                    "--bind",
                    "-o",
                    "ro",
                    volume_root,
                    target_path,
                ]
            else:
                # For readwrite we use an overlayfs mount, the benefit here is that
                # it works as CoW even if the underlying filesystem doesn't support
                # it, reducing host storage usage.
                workdir = volume_root / "workdir"
                upperdir = volume_root / "upperdir"
                workdir.mkdir(parents=True, exist_ok=True)
                upperdir.mkdir(parents=True, exist_ok=True)
                mount_command = [
                    "mount",
                    "--verbose",
                    "-t",
                    "overlay",
                    "overlay",
                    "-o",
                    f"rw,lowerdir={volume_root},upperdir={upperdir},workdir={workdir}",
                    target_path,
                ]

            mount = await run_console(*mount_command)
            if mount.returncode == MOUNT_ALREADY_MOUNTED:
                logger.debug(f"Mount target {target_path} was already mounted")
            elif mount.returncode != 0:
                # Clean up resources on mount failure
                shutil.rmtree(gc_root, ignore_errors=True)
                shutil.rmtree(volume_root, ignore_errors=True)
                raise GRPCError(
                    Status.INTERNAL,
                    f"Failed to mount {mount.returncode=} {mount.stderr=}",
                )

            reply = csi_pb2.NodePublishVolumeResponse()
            await stream.send_message(reply)

            # Rework this horrible cache bullshit
            if primary_package_path.exists():
                task = asyncio.create_task(copyToCache(primary_package_path))
                task.add_done_callback(
                    lambda t: logger.error(f"copyToCache failed: {t.exception()}")
                    if t.exception()
                    else None
                )

    @staticmethod
    async def IsMount(path: Path):
        return (await run_captured("findmnt", "--mountpoint", path)).returncode == 0

    @staticmethod
    async def Unmount(path: Path):
        return await run_captured("umount", "--verbose", path)

    @csi_error_handler
    async def NodeUnpublishVolume(self, stream):
        request: csi_pb2.NodeUnpublishVolumeRequest | None = await stream.recv_message()
        if request is None:
            raise ValueError("NodeUnpublishVolumeRequest is None")

        logger.info(f"Unpublish {request.target_path}")

        async with self.volumeLocks[request.volume_id]:
            target_path = Path(request.target_path)

            # Cleanup operations are intentionally fail-fast (not wrapped in individual try/except).
            # Kubelet will retry NodeUnpublishVolume indefinitely on failure, so we want to
            # stop at the first error and let the retry start from scratch. This is safer than
            # attempting partial cleanup, as each retry re-attempts all steps in order.

            # Unmount
            if await NodeServicer.IsMount(target_path):
                umount = await NodeServicer.Unmount(target_path)
                if umount.returncode != 0 and await NodeServicer.IsMount(target_path):
                    raise GRPCError(
                        Status.INTERNAL, "unmount failed", f"{umount.combined=}"
                    )
                else:
                    logger.debug(f"unmounted {request.target_path=}")

            # Remove mount dir
            if target_path.exists():
                try:
                    target_path.rmdir()
                    logger.debug(f"removed {target_path=}")
                except Exception as ex:
                    raise GRPCError(
                        Status.INTERNAL, f"removing {target_path=} failed", ex
                    )

            # Remove gcroots
            gc_root = CSI_GCROOTS / request.volume_id
            if gc_root.exists():
                try:
                    shutil.rmtree(gc_root, ignore_errors=True)
                    logger.debug(f"unlinked {gc_root=}")
                except Exception as ex:
                    raise GRPCError(
                        Status.INTERNAL, f"unlinking {target_path=} failed", ex
                    )

            # Remove hardlink farm
            volume_path = CSI_VOLUMES / request.volume_id
            if volume_path.exists():
                try:
                    shutil.rmtree(volume_path)
                    logger.debug(f"removed {volume_path=}")
                except Exception as ex:
                    raise GRPCError(
                        Status.INTERNAL, f"recursive removing {target_path=} failed", ex
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
            raise GRPCError(
                Status.FAILED_PRECONDITION,
                "KUBE_NODE_NAME environment variable not set",
            )
        reply = csi_pb2.NodeGetInfoResponse(node_id=node_name)
        await stream.send_message(reply)

    async def NodeGetVolumeStats(self, stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeGetVolumeStats not implemented")

    async def NodeExpandVolume(self, stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeExpandVolume not implemented")

    async def NodeStageVolume(self, stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeStageVolume not implemented")

    async def NodeUnstageVolume(self, stream):
        raise GRPCError(Status.UNIMPLEMENTED, "NodeUnstageVolume not implemented")


async def serve():
    sock_path = "/csi/csi.sock"
    Path(sock_path).unlink(missing_ok=True)

    identityServicer = IdentityServicer()
    nodeServicer = NodeServicer(await get_current_system())

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
