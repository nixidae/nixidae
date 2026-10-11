"""Grains for Nix and NixOS, so the master can target by what a minion runs:

    salt -G 'nixos:system:/nix/store/...-nixos-system-web1-26.05' test.ping
    salt -G 'nix:daemon:True' havsorm.collect_garbage

Grains run at every minion start and refresh, so these read links and
files only. None of them opens a store.
"""

import os
from typing import Any


def _target(link: str) -> str | None:
    return os.path.realpath(link) if os.path.lexists(link) else None


def nix() -> dict[str, Any]:
    """`nix`: the Nix havsorm links, and the state of this host's store."""
    try:
        import huggorm_bindings

        linked = {"version": huggorm_bindings.nix_version(),
                  "system": huggorm_bindings.current_system()}
    except ImportError:
        linked = {}
    return {"nix": {
        **linked,
        "store": os.path.isdir("/nix/store"),
        "daemon": os.path.exists("/nix/var/nix/daemon-socket/socket"),
        "profile": _target("/nix/var/nix/profiles/default"),
    }}


def nixos() -> dict[str, Any]:
    """`nixos`: the running, booted and next-boot systems of a NixOS host.
    Absent elsewhere."""
    running = _target("/run/current-system")
    if running is None or not os.path.isfile(os.path.join(running, "nixos-version")):
        return {}
    with open(os.path.join(running, "nixos-version")) as f:
        version = f.read().strip()
    return {"nixos": {
        "system": running,
        "booted": _target("/run/booted-system"),
        "profile": _target("/nix/var/nix/profiles/system"),
        "version": version,
    }}
