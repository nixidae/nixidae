# SPDX-License-Identifier: MIT

import asyncio
import os
from pathlib import Path

import structlog
from pynixd.config import PynixdSettings
from pynixd.instance import Server
from pynixd.store import LocalSocketStore

from .builder_manager import BuilderManager
from .setup import configure_logging, install_nss

log = structlog.get_logger(__name__)


async def _main() -> None:
    configure_logging()
    log.info("pynixd_nixkube_central_starting")
    install_nss()

    local_store = LocalSocketStore(
        store_id="local",
        store_path=Path("/"),
        use_db=False,
        monitor=False,
    )

    settings = PynixdSettings()

    server = Server(local_store=local_store, settings=settings)

    builder_manager: BuilderManager | None = None

    async with server:
        if settings.schedule_mode != "scheduler":
            await server.add_store(local_store)

        namespace = os.environ.get("KUBE_NAMESPACE")
        max_builders = int(os.environ.get("BUILDER_MAX", "3"))

        if server.scheduler:
            server.scheduler.add_dynamic_features(
                {
                    "x86_64-linux": {"nixos-test", "big-parallel", "benchmark"},
                }
            )
            log.info(
                "dynamic_features_registered",
                features=server.scheduler.dynamic_feature_matrix,
            )

        if namespace:
            builder_manager = BuilderManager(
                server=server,
                namespace=namespace,
                max_builders=max_builders,
            )
            await builder_manager.start()

        log.info("pynixd_nixkube_central_running")
        try:
            await server.wait_finished()
        finally:
            if builder_manager:
                await builder_manager.stop()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
