# SPDX-License-Identifier: MIT

from pathlib import Path

import structlog
from shellous import sh

from ..errors import InitDatabaseError

logger = structlog.get_logger("nixkube.nix")


async def init_database(state_dir: Path, store_paths: set[Path]) -> None:
    """
    Initialize the Nix database for a chroot store by piping dump to load.

    Equivalent to: nix-store --dump-db <paths> | NIX_STATE_DIR=<state_dir> nix-store --load-db
    """
    try:
        await (
            sh(
                "nix-store",
                "--option",
                "store",
                "local",
                "--dump-db",
                *store_paths,
            )
            | sh(
                "nix-store",
                "--option",
                "store",
                "local",
                "--load-db",
            ).env(NIX_STATE_DIR=str(state_dir), USER="nobody")
        )
    except Exception as e:
        raise InitDatabaseError(
            "Failed to initialize Nix database",
            logs=str(e),
        ) from e

    logger.debug(
        "nix_database_initialized", state_dir=str(state_dir), count=len(store_paths)
    )
