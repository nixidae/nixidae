# SPDX-License-Identifier: MIT

import asyncio
from pathlib import Path

import structlog
from pynixd.config import PynixdSettings
from pynixd.instance import Server
from pynixd.store import LocalSocketStore

from .setup import configure_logging, install_nss

log = structlog.get_logger(__name__)


async def _main():
    configure_logging()
    log.info("pynixd_nixkube_starting")
    install_nss()

    local_store = LocalSocketStore(
        store_id="local",
        store_path=Path("/"),
        use_db=False,
        monitor=False,
    )

    settings = PynixdSettings()

    server = Server(local_store=local_store, settings=settings)

    async with server:
        await server.add_store(local_store)
        log.info("pynixd_nixkube_running")
        await server.wait_finished()


def main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
