"""What every phase of frr-clos asks of the fabric."""

from __future__ import annotations

import json
from typing import Any

from vivarium_runner import Machine, Machines

CONVERGE = 90.0
"""Seconds for BGP to settle after boot or a link change."""

FAMILIES = ("ipv4Unicast", "l2VpnEvpn")


def switches(vms: Machines) -> dict[str, Machine]:
    clos = vms.settings["clos"]
    return {name: vms[name] for name in [*clos["spines"], *clos["leaves"]]}


def loopbacks(vms: Machines) -> dict[str, str]:
    clos = vms.settings["clos"]
    return {name: one["loopback"] for name, one in {**clos["spines"], **clos["leaves"]}.items()}


async def vtysh(vm: Machine, command: str) -> dict[str, Any]:
    """Run a `vtysh` command with `json` appended, and parse its object."""
    value: object = json.loads(await vm.succeed(f"vtysh -c '{command} json'"))
    if not isinstance(value, dict):
        raise AssertionError(f"vtysh {command!r} printed {type(value).__name__}, not an object")
    return value


async def bgp_down(vm: Machine) -> str:
    """Each BGP peer of *vm* that is not up, and each family with no peers; empty when all are up."""
    summary = await vtysh(vm, "show bgp summary")
    down = [
        f"{family}/{peer}: {state['state']}"
        for family in FAMILIES
        for peer, state in summary.get(family, {}).get("peers", {}).items()
        if state["state"] != "Established"
    ]
    down += [f"{family}: no peers" for family in FAMILIES if not summary.get(family, {}).get("peers")]
    return "\n".join(down)


async def bgp_up(vm: Machine) -> bool | None:
    return None if await bgp_down(vm) else True


async def established(vm: Machine, timeout: float = CONVERGE) -> None:
    """Every BGP peer of *vm* up, for IPv4 and for EVPN."""
    await vm.until("BGP established, IPv4 and EVPN", lambda: bgp_up(vm), timeout, evidence=lambda: bgp_down(vm))


def nexthops(route: str) -> int:
    """Paths in `ip route show <prefix>` output: ECMP lists each as `nexthop`."""
    many = route.count("nexthop")
    return many if many else (1 if route.strip() else 0)


def ping(address: str, source: str | None = None) -> str:
    return f"ping -c 1 -W 2 {f'-I {source} ' if source else ''}{address}"
