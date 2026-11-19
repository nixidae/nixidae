"""Helpers for tests of vivarium and of the runner.

A whole machine spec, as `machineSpec` in lib.nix writes it, for a test to change one field of.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .machine_spec import MachineSpec
from .spec import BackendName, Seccomp, UplinkProxy

if TYPE_CHECKING:
    from .machine_spec import MachineSpecJson


def machine_json(name: str, backend: BackendName = BackendName.UML) -> MachineSpecJson:
    return {
        "name": name,
        "backend": backend,
        "kind": "nixos",
        "index": 0,
        "image": f"/nix/store/{name}-root",
        "memory": "128M",
        "seccomp": Seccomp.AUTO,
        "cpus": 1,
        "sshPort": 22,
        "mtu": 65000,
        "network": None,
        "address": None,
        "control": {"kind": "agent", "config": {}},
        "interfaces": [],
        "uplink": None,
        "uplinkProxy": UplinkProxy.PASST,
        "uplinkNetwork": {
            "address": "10.0.2.15",
            "prefixLength": 24,
            "gateway": "10.0.2.2",
            "dns": "10.0.2.3",
            "gateway6": "fd00:10:0:2::2",
        },
        "router": False,
        "inbound": [],
        "boot": None,
        "nested": False,
        "display": None,
    }


def machine_spec(name: str, backend: BackendName = BackendName.UML, **fields: Any) -> MachineSpec:
    return replace(MachineSpec.from_json(machine_json(name, backend)), **fields)
