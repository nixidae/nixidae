# SPDX-License-Identifier: MIT

import logging
import subprocess
import tempfile
from functools import cache
from pathlib import Path

from kr8s.asyncio.objects import Pod

from .builders import build_builder_args, get_builder_uris
from .cache import check_cache_connectivity, get_substituter_args
from .constants import NIX_BUILD_TIMEOUT
from .errors import (
    BuildError,
    CommandTimeoutError,
    InitDatabaseError,
    InstallGCRootError,
    InstallResultLinkError,
    StorePathClosureError,
    SubprocessError,
    SystemDetectionError,
    VerifyStorePathsError,
)
from .store import extract_store_name, extract_store_paths
from .subprocessing import try_captured, try_console

logger = logging.getLogger("nix-csi")


@cache
def get_current_system() -> str:
    """Get system string evaluated by nix (cached after first call)."""
    try:
        result = subprocess.run(
            [
                "nix",
                "eval",
                "--raw",
                "--impure",
                "--store",
                "dummy://",
                "--expr",
                "builtins.currentSystem",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        raise SystemDetectionError(
            "Failed to detect system type",
            logs=str(e),
        ) from e


async def get_build_args() -> list[str]:
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


async def get_closure_paths(package_paths: set[Path]) -> set[Path]:
    """Get all store paths in the closure of the given packages."""
    try:
        return {
            Path(p)
            for p in (
                await try_captured(
                    "nix",
                    "path-info",
                    "--recursive",
                    *package_paths,
                )
            ).stdout.splitlines()
        }
    except SubprocessError as e:
        raise StorePathClosureError(
            "Failed to get store path closure",
            logs=e.combined,
        ) from e


async def verify_store_paths(package_paths: set[Path]) -> None:
    """Verify the integrity of all packages and their closures."""
    try:
        await try_captured(
            "nix",
            "store",
            "verify",
            "--recursive",
            *package_paths,
        )
    except SubprocessError as e:
        raise VerifyStorePathsError(
            "Failed to verify store paths",
            logs=e.combined,
        ) from e


async def build_store_path(
    store_path: str,
    gc_root: Path,
    extra_args: list[str],
    timeout: float = NIX_BUILD_TIMEOUT,
) -> Path:
    """Build/fetch a store path and create a gc root."""
    try:
        name = extract_store_name(store_path)
        result = await try_console(
            "nix",
            "build",
            *extra_args,
            "--print-out-paths",
            "--out-link",
            gc_root / name,
            store_path,
            timeout=timeout,
        )
        return Path(result.stdout.splitlines()[0])
    except CommandTimeoutError as e:
        raise BuildError(
            f"Build timeout for {store_path} after {timeout}s",
            logs=e.combined,
        ) from e
    except SubprocessError as e:
        raise BuildError(
            f"Failed to build store path {store_path}",
            logs=e.combined,
        ) from e


async def build_flake_ref(
    flake_ref: str,
    gc_root: Path,
    extra_args: list[str],
    timeout: float = NIX_BUILD_TIMEOUT,
) -> Path:
    """Build a flake reference and create a gc root."""
    try:
        result = await try_console(
            "nix",
            "build",
            *extra_args,
            "--print-out-paths",
            "--out-link",
            gc_root / "flake",
            flake_ref,
            timeout=timeout,
        )
        return Path(result.stdout.splitlines()[0])
    except CommandTimeoutError as e:
        raise BuildError(
            f"Build timeout for flake {flake_ref} after {timeout}s",
            logs=e.combined,
        ) from e
    except SubprocessError as e:
        raise BuildError(
            f"Failed to build flake {flake_ref}",
            logs=e.combined,
        ) from e


async def build_nix_expr(
    nix_expr: str,
    gc_root: Path,
    extra_args: list[str],
    timeout: float = NIX_BUILD_TIMEOUT,
) -> Path:
    """Build a Nix expression and create a gc root."""
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".nix") as tmp:
            tmp.write(nix_expr)
            tmp.flush()

            result = await try_console(
                "nix",
                "build",
                *extra_args,
                "--print-out-paths",
                "--out-link",
                gc_root / "expr",
                "--file",
                tmp.name,
                timeout=timeout,
            )
            return Path(result.stdout.splitlines()[0])
    except CommandTimeoutError as e:
        raise BuildError(
            f"Build timeout for Nix expression after {timeout}s",
            logs=e.combined,
        ) from e
    except SubprocessError as e:
        raise BuildError(
            "Failed to build Nix expression",
            logs=e.combined,
        ) from e


async def init_database(state_dir: Path, store_paths: set[Path]) -> None:
    """
    Initialize the Nix database for a chroot store.
    This runs nix-store --dump-db | NIX_STATE_DIR=something nix-store --load-db
    """
    try:
        await try_captured(
            "nix_init_db",
            state_dir,
            *store_paths,
        )
    except SubprocessError as e:
        raise InitDatabaseError(
            "Failed to initialize Nix database",
            logs=e.combined,
        ) from e


async def install_gcroots(
    package_paths: set[Path],
    out_link: Path,
    store: Path | None = None,
    timeout: float | None = None,
) -> None:
    """Install gc roots with single batch build."""
    if not package_paths:
        return

    try:
        args: list[str | Path] = ["nix", "build"]
        if store is not None:
            args.extend(["--store", store])
        args.extend(["--out-link", out_link])
        args.extend(package_paths)

        if timeout is not None:
            await try_captured(*args, timeout=timeout)
        else:
            await try_captured(*args)
    except CommandTimeoutError as e:
        raise InstallGCRootError(
            f"GC root installation timeout after {timeout}s",
            logs=e.combined,
        ) from e
    except SubprocessError as e:
        raise InstallGCRootError(
            "Failed to install garbage collection root",
            logs=e.combined,
        ) from e


async def install_result_link(
    volume_root: Path,
    package_path: Path,
) -> None:
    """Install /nix/var/result symlink in the chroot store."""
    try:
        await try_captured(
            "nix",
            "build",
            "--store",
            volume_root,
            "--out-link",
            volume_root / "nix/var/result",
            package_path,
            timeout=NIX_BUILD_TIMEOUT,
        )
    except CommandTimeoutError as e:
        raise InstallResultLinkError(
            f"Result link installation timeout after {NIX_BUILD_TIMEOUT}s",
            logs=e.combined,
        ) from e
    except SubprocessError as e:
        raise InstallResultLinkError(
            "Failed to install /nix/var/result symlink",
            logs=e.combined,
        ) from e


async def build_packages(
    package_paths: set[Path],
    gc_root: Path,
    extra_args: list[str] = [],
) -> set[Path]:
    """Batch build packages with a single nix build call."""
    if not package_paths:
        return set()

    gc_root.mkdir(parents=True, exist_ok=True)

    # Batch build all packages with single nix build call
    args: list[str | Path] = ["nix", "build"]
    args.extend(extra_args)
    args.extend(["--out-link", gc_root / "build"])
    args.extend(package_paths)

    try:
        await try_console(*args, timeout=NIX_BUILD_TIMEOUT)
    except SubprocessError as e:
        logger.error(
            "Failed to build packages\n"
            + f"Command: {e.command}\n"
            + "Logs:\n"
            + e.combined
        )
        raise
    except Exception as e:
        logger.error(f"Failed to build packages: {e}")
        raise

    logger.debug(f"Built {len(package_paths)} packages")
    return package_paths


async def build_pod_packages(
    pod: Pod,
    gc_root: Path,
    extra_args: list[str],
) -> set[Path]:
    """Extract and batch build packages referenced in the pod spec."""
    pod_store_paths = extract_store_paths(pod.raw)

    if not pod_store_paths:
        return set()

    return await build_packages(pod_store_paths, gc_root, extra_args)


async def build_primary_package(
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
        logger.debug(f"{store_path=}")
        return await build_store_path(
            store_path,
            gc_root,
            extra_args,
            timeout=NIX_BUILD_TIMEOUT,
        )

    if flake_ref is not None:
        logger.debug(f"{flake_ref=}")
        return await build_flake_ref(
            flake_ref,
            gc_root,
            extra_args,
            timeout=NIX_BUILD_TIMEOUT,
        )

    if nix_expr is not None:
        logger.debug(f"{nix_expr=}")
        return await build_nix_expr(
            nix_expr,
            gc_root,
            extra_args,
            timeout=NIX_BUILD_TIMEOUT,
        )

    return None
