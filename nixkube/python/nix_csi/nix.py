import tempfile
from pathlib import Path

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
from .store import extract_store_name
from .subprocessing import try_captured, try_console


async def get_current_system() -> str:
    """Get system string evaluated by nix."""
    try:
        return (
            await try_captured(
                "nix", "eval", "--raw", "--impure", "--expr", "builtins.currentSystem"
            )
        ).stdout
    except SubprocessError as e:
        raise SystemDetectionError(
            "Failed to detect system type",
            logs=e.combined,
        ) from e


async def get_closure_paths(package_paths: list[Path]) -> list[str]:
    """Get all store paths in the closure of the given packages."""
    try:
        return (
            await try_captured(
                "nix",
                "path-info",
                "--recursive",
                *package_paths,
            )
        ).stdout.splitlines()
    except SubprocessError as e:
        raise StorePathClosureError(
            "Failed to get store path closure",
            logs=e.combined,
        ) from e


async def verify_store_paths(package_paths: list[Path]) -> None:
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


async def init_database(state_dir: Path, store_paths: list[str]) -> None:
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
    package_paths: list[Path],
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
