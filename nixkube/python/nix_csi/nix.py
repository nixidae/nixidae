import tempfile
from pathlib import Path

from .constants import NIX_BUILD_TIMEOUT
from .errors import (
    ExprBuildError,
    FlakeBuildError,
    PathBuildError,
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


async def verify_store_paths(store_paths: list[str]) -> None:
    """Verify the integrity of all store paths in the closure."""
    try:
        await try_captured(
            "nix",
            "store",
            "verify",
            "--recursive",
            *store_paths,
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
    except SubprocessError as e:
        raise PathBuildError(
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
    except SubprocessError as e:
        raise FlakeBuildError(
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
    except SubprocessError as e:
        raise ExprBuildError(
            "Failed to evaluate Nix expression",
            logs=e.combined,
        ) from e


async def init_database(state_dir: Path, store_paths: list[str]) -> None:
    """
    Initialize the Nix database for a chroot store.
    This runs nix-store --dump-db | NIX_STATE_DIR=something nix-store --load-db
    """
    await try_captured(
        "nix_init_db",
        state_dir,
        *store_paths,
    )


async def install_gcroot(
    volume_root: Path,
    package_path: Path,
    name: str,
    state_dir: Path,
) -> None:
    """Install a gc root in the chroot store."""
    await try_captured(
        "nix",
        "build",
        "--store",
        volume_root,
        "--out-link",
        state_dir / f"gcroots/{name}",
        package_path,
    )


async def install_result_link(
    volume_root: Path,
    package_path: Path,
) -> None:
    """Install /nix/var/result symlink in the chroot store."""
    await try_captured(
        "nix",
        "build",
        "--store",
        volume_root,
        "--out-link",
        volume_root / "nix/var/result",
        package_path,
    )
