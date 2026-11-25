"""A guest a phase creates from a template while the run goes. nixidae/nixidae#1408.

Pure: what an instance is called, which index and addresses it gets, and
the seed it reads at boot. `vivarium.Session.launch` does the effects.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Collection, Mapping
from dataclasses import replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .machine_spec import MachineSpec

MAX_INDEX = 255
"""The index is the last byte of every MAC the guest has, so 256 guests at once."""

LAN = "vec1"
"""The interface `vivarium.lan` makes: its first address is the guest's `address`."""


class InstanceError(ValueError):
    """A launch this run cannot do: no such template, a name taken, no index free."""


def free_index(taken: Collection[int], first: int) -> int:
    """The lowest index from *first* that no guest holds. A terminated instance's index comes back."""
    for index in range(first, MAX_INDEX + 1):
        if index not in taken:
            return index
    raise InstanceError(f"every index from {first} to {MAX_INDEX} is taken: a run holds at most {MAX_INDEX + 1} guests")


def shifted(addresses: Collection[str], slot: int) -> tuple[str, ...]:
    """Each of *addresses* plus *slot*, prefix length kept: `10.9.0.100/24`, slot 2 -> `10.9.0.102/24`."""
    out = []
    for text in addresses:
        interface = ipaddress.ip_interface(text)
        address = interface.ip + slot
        if address not in interface.network:
            raise InstanceError(f"slot {slot} puts {text} outside its network {interface.network}")
        out.append(f"{address}/{interface.network.prefixlen}")
    return tuple(out)


def instance_spec(
    template: MachineSpec, name: str, index: int, segment_addresses: Mapping[str, Collection[str]], slot: int
) -> MachineSpec:
    """*template* as the guest *name*: its own index, the MACs that carry it, its addresses, no host forwards.

    A forward binds a host port, and two instances of one template would bind the same one.
    The first address on `LAN` is the instance's `address`, as `vivarium.lan.address` is a node's.
    """
    interfaces = tuple(
        replace(
            nic,
            mac=f"52:54:00:12:{nic.nic:02x}:{index:02x}",
            addresses=shifted(segment_addresses.get(nic.segment, ()), slot),
        )
        for nic in template.interfaces
    )
    lan = next((nic.addresses[0] for nic in interfaces if nic.name == LAN and nic.addresses), None)
    return replace(template, name=name, index=index, interfaces=interfaces, inbound=(), address=lan)


def seed(spec: MachineSpec, template: str, user_data: str | None) -> dict[str, object]:
    """What the guest's `vivarium-instance` unit reads from `/artifacts/instance.json`."""
    return {
        "name": spec.name,
        "template": template,
        "index": spec.index,
        "interfaces": {nic.name: list(nic.addresses) for nic in spec.interfaces},
        "user_data": user_data,
    }
