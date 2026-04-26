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

FAKE_NSS = Path(os.environ["FAKE_NSS"])


def install_nss() -> None:
    for name in ("passwd", "group", "nsswitch.conf"):
        src = FAKE_NSS / "etc" / name
        dst = Path(f"/etc/{name}")
        if src.exists() and not dst.exists():
            dst.write_text(src.read_text())
            log.info("installed_nss_file", name=name)

    sh_dst = Path("/bin/sh")
    if not sh_dst.exists():
        sh_dst.parent.mkdir(parents=True, exist_ok=True)
        sh_dst.symlink_to("/nix/var/result/bin/bash")
        log.info("installed_bin_sh", target="/nix/var/result/bin/bash")


async def _main():
    if os.environ.get("KUBERNETES_SERVICE_HOST"):
        structlog.configure(
            processors=[
                structlog.processors.TimeStamper(fmt="iso"),
                structlog.processors.JSONRenderer(),
            ]
        )

    log.info("pynixd_nixkube_starting")
    install_nss()

    local_store = LocalSocketStore(
        store_id="local",
        store_path=Path("/"),
        use_db=False,
        monitor=False,
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
