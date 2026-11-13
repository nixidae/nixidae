"""The way into a run from the host, written to its `--out`.

    access.json  each guest's ssh address and forwards; the two paths below
    ssh_config   `ssh -F <out>/ssh_config <guest>`
    access/      what a phase writes for the host: a kubeconfig, a token

The runner picks a guest's forward address when the run starts
(`127.0.0.2` upwards), so Nix cannot write these; the session writes them
once the forwards are settled, before any guest boots.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from vivarium_runner.forward import Rule

FILE: Final = "access.json"
SSH_CONFIG: Final = "ssh_config"
DIRECTORY: Final = "access"


@dataclass(frozen=True, slots=True)
class Guest:
    ssh: str | None
    """`address:port` of the guest's sshd on the host; `None` when nothing forwards it."""
    forwards: tuple[Rule, ...]


def guest(ssh: list[str], forwards: list[Rule]) -> Guest:
    """One guest's way in, from `Machine.reachable(ssh_port)` and its settled forwards."""
    return Guest(ssh=ssh[0] if ssh else None, forwards=tuple(forwards))


def document(guests: Mapping[str, Guest], out: Path) -> str:
    def rule(each: Rule) -> dict[str, object]:  # object: one JSON value of several kinds
        ports: object = (
            "all" if isinstance(each.ports, str) else [{"host": p.host, "guest": p.guest} for p in each.ports]
        )
        return {"address": each.address, "ports": ports, "protocols": list(each.protocols)}

    return (
        json.dumps(
            {
                "guests": {
                    name: {"ssh": one.ssh, "forwards": [rule(each) for each in one.forwards]}
                    for name, one in guests.items()
                },
                "ssh_config": str(out / SSH_CONFIG),
                "files": str(out / DIRECTORY),
            },
            indent=2,
        )
        + "\n"
    )


def ssh_config(guests: Mapping[str, Guest]) -> str:
    """A `Host` block per guest with ssh. A guest's host key is new on each
    boot, and its address is reused by the next run, so nothing is recorded."""
    blocks = []
    for name, one in guests.items():
        if one.ssh is None:
            continue
        address, _, port = one.ssh.rpartition(":")
        blocks.append(
            f"Host {name}\n"
            f"  HostName {address}\n"
            f"  Port {port}\n"
            "  User root\n"
            "  StrictHostKeyChecking no\n"
            "  UserKnownHostsFile /dev/null\n"
            "  LogLevel ERROR\n"
        )
    return "\n".join(blocks)


def write(guests: Mapping[str, Guest], out: Path) -> Path:
    """Both files, and an empty `access/`: a reused `--out` must not hand
    over the last run's kubeconfig, whose address may be another run's now."""
    (out / FILE).write_text(document(guests, out))
    (out / SSH_CONFIG).write_text(ssh_config(guests))
    directory = out / DIRECTORY
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    return directory
