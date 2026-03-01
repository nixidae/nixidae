# SPDX-License-Identifier: MIT

import logging
import tempfile
from pathlib import Path

from kr8s.asyncio.objects import Pod

from ..builders import build_builder_args, get_builder_uris
from ..cache import check_cache_connectivity, get_substituter_args
from ..constants import NIX_BUILD_TIMEOUT
from ..errors import BuildError, CommandTimeoutError, SubprocessError
from ..store import extract_store_name, extract_store_paths
from ..subprocessing import try_console

logger = logging.getLogger("nix-csi")


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
