# SPDX-License-Identifier: MIT

import logging
from pathlib import Path

from shellous import sh

from ..errors import InitDatabaseError

logger = logging.getLogger("nixkube.nix")


async def init_database(state_dir: Path, store_paths: set[Path]) -> None:
    """
    Initialize the Nix database for a chroot store by piping dump to load.

    Equivalent to: nix-store --dump-db <paths> | NIX_STATE_DIR=<state_dir> nix-store --load-db
    """
    try:
        # Use shellous pipeline to pipe dump to load
        # dump: nix-store --dump-db <store_paths>
        # load: nix-store --load-db with NIX_STATE_DIR set
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
            )

        logger.debug(
            f"Initialized Nix database at {state_dir} with {len(store_paths)} paths"
        )

    except InitDatabaseError:
        raise
    except Exception as e:
        raise InitDatabaseError(
            "Failed to initialize Nix database",
            logs=str(e),
        ) from e
