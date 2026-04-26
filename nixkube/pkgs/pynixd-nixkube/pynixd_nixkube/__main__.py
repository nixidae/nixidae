# SPDX-License-Identifier: MIT

import asyncio
import os
from pathlib import Path

import structlog
from environs import Env
from pynixd.config import PynixdSettings
from pynixd.instance import Server
from pynixd.store import LocalSocketStore

env = Env()
env.read_env()

log = structlog.get_logger(__name__)


async def _main():
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ]
        )

    log.info("pynixd_nixkube_starting")

    local_store = LocalSocketStore(
        store_id="local",
        store_path=Path("/"),
        use_db=False,
        monitor=False,
        extra_args=["--force-trusted"],
    )

    settings = PynixdSettings(
        ssh_port=env.int("PYNIXD_SSH_PORT", 2222),
        http_port=env.int("PYNIXD_HTTP_PORT", 8080),
    )

    server = Server(local_store=local_store, settings=settings)

    async with server:
        log.info("pynixd_nixkube_running")
        await server.wait_finished()


def main():
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
