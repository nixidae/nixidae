# SPDX-License-Identifier: MIT

"""SSH authorized key file watching and dynamic reloading.

Watches authorized_keys files for changes using inotify and applies
updates to the asyncssh server without restarting.
"""

import os
from pathlib import Path

import structlog
from asyncinotify import Inotify, Mask
from pynixd.instance import Server

log = structlog.get_logger(__name__)

_DEFAULT_FILES = "/etc/ssh/authorized_keys:/etc/ssh-dynauth/authorized_keys"
_KEY_FILES = [
    Path(p)
    for p in os.environ.get("NIXKUBE_AUTHORIZED_KEYS_FILES", _DEFAULT_FILES).split(":")
    if p
]
_HOST_KEY = "/etc/ssh-key/id_ed25519"


def _collect_key_paths() -> list[str]:
    return [str(p) for p in _KEY_FILES if p.is_file()]


def apply_authorized_keys(server: Server) -> None:
    """Re-read authorized key files from disk and update the SSH server.

    Safe to call repeatedly — asyncssh's ``update()`` re-parses the files
    each time and applies changes to all future connections.
    """
    paths = _collect_key_paths()
    if paths:
        server.ssh_server.update(authorized_client_keys=paths)
    server.ssh_server.update(server_host_keys=[_HOST_KEY])
    log.info("ssh_authorized_keys_loaded", count=len(paths))


async def watch_authorized_keys(server: Server) -> None:
    """Background task that uses inotify to reload authorized keys on change.

    Kubernetes ConfigMap updates are atomic symlink swaps — the files stay at
    the same path but their target inode changes.  We watch the *parent
    directory* for ``MOVED_TO`` / ``CREATE``, then re-read the key files.
    """
    apply_authorized_keys(server)

    filenames = {p.name for p in _KEY_FILES}
    parents: set[Path] = set()
    for p in _KEY_FILES:
        parent = p.parent
        if parent.is_dir():
            parents.add(parent)

    with Inotify() as inotify:
        for d in parents:
            inotify.add_watch(d, Mask.MOVED_TO | Mask.CREATE)

        async for event in inotify:
            if event.name is None:
                continue
            if event.name.name in filenames:
                apply_authorized_keys(server)
