# SPDX-License-Identifier: MIT

import asyncio
from pathlib import Path

import structlog
from pynixd.config import PynixdSettings
from pynixd.instance import Server
from pynixd.store import LocalSocketStore

from .builder_manager import BuilderManager
from .config import NixkubeCentralSettings
from .setup import configure_logging, install_nss
from .ssh_keys import watch_authorized_keys

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

    pynixd_settings = PynixdSettings()
    settings = NixkubeCentralSettings()

    server = Server(local_store=local_store, settings=pynixd_settings)

    builder_manager: BuilderManager | None = None

    async with server:
        keys_watch = asyncio.create_task(watch_authorized_keys(server))
        server.background_tasks.append(keys_watch)

        if pynixd_settings.schedule_mode != "scheduler":
            await server.add_store(local_store)

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

        if settings.kube_namespace:
            builder_manager = BuilderManager(
                server=server,
                namespace=settings.kube_namespace,
                max_builders=settings.builder_max,
                min_builders=settings.builder_min,
                idle_timeout=settings.idle_timeout,
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
