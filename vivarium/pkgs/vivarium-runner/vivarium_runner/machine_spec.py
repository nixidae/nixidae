"""A guest's spec: what Nix knows about it, as the runner reads it.

`MachineSpecJson` is the shape `machineSpec` in lib.nix writes.
`vivarium.spec` checks a spec against it with pydantic, and
`MachineSpec.from_json` turns it into the runner's values.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, NotRequired, TypedDict

from . import forward, sysctl
from .boot import Boot, BootJson, Display, DisplayJson
from .control import ControlSpec, ControlSpecJson
from .forward import UplinkNetwork, UplinkNetworkJson
from .spec import BackendName, Seccomp, UplinkProxy, closed


@closed
class InterfaceJson(TypedDict):
    """`Interface` as Nix wrote it."""

    name: str
    nic: int
    segment: str
    addresses: list[str]
    mac: str
    mtu: int


@closed
class CpuScale(TypedDict):
    """`vivarium.cpus = { scale = s; }`: the host's cores times `s`."""

    scale: float


Cpus = int | CpuScale
"""`vivarium.cpus` as Nix wrote it: a count, or a share of the host."""


def host_cpus(environ: Mapping[str, str] = os.environ) -> int:
    """The cores this run may use: `NIX_BUILD_CORES` inside a build (0 is
    every core), else the cores this process may run on."""
    granted = environ.get("NIX_BUILD_CORES", "")
    if granted.isdigit() and int(granted) > 0:
        return int(granted)
    # The affinity mask on Linux; macOS has none, and counts every core.
    # Not os.process_cpu_count: it needs Python 3.13, and this package runs on 3.12.
    if sys.platform == "linux":
        return len(os.sched_getaffinity(0))
    return os.cpu_count() or 1


def resolve_cpus(cpus: Cpus, host: int) -> int:
    """A guest's processor count, from `vivarium.cpus` and the host's cores."""
    if isinstance(cpus, int):
        return cpus
    return max(1, round(host * cpus["scale"]))


@closed
class MemoryScale(TypedDict):
    """`vivarium.memory = { scale = s; }`: the host's available memory times `s`."""

    scale: float


Memory = str | MemoryScale
"""`vivarium.memory` as Nix wrote it: a size such as `"512M"`, or a share of the host."""

MEMORY_SCALE_FLOOR_MIB = 512
"""The least a scaled guest gets: the krun and QEMU default size, which boots."""


def host_available_mib(meminfo: Path | None = None) -> int:
    """`MemAvailable` of the host, in MiB. On macOS, the share of memory
    the kernel counts as free (`kern.memorystatus_level`, the figure
    `memory_pressure` prints) of `hw.memsize`."""
    if meminfo is None and sys.platform == "darwin":
        return sysctl.read("hw.memsize") * sysctl.read("kern.memorystatus_level") // 100 >> 20
    meminfo = meminfo or Path("/proc/meminfo")
    for line in meminfo.read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    raise RuntimeError(f"{meminfo} has no MemAvailable")


def resolve_memory(memory: Memory, available_mib: Callable[[], int] = host_available_mib) -> str:
    """A guest's memory size, from `vivarium.memory` and the host's available memory at start.

    A scaled guest's size is a ceiling and not a cost: krun and QEMU back
    only the pages the guest touches, and free page reporting gives freed
    ones back."""
    if isinstance(memory, str):
        return memory
    return f"{max(MEMORY_SCALE_FLOOR_MIB, round(available_mib() * memory['scale']))}M"


@closed
class MachineSpecJson(TypedDict):
    """`MachineSpec` as Nix wrote it: `machineSpec` in lib.nix.

    `vivarium.spec` checks a spec against it with pydantic. A key is
    `NotRequired` only where lib.nix leaves it out for some guests.
    """

    name: str
    backend: BackendName
    kind: str
    """Open: `nixos`, or a name from `vivarium.kinds`, which a module may add to."""
    index: int
    image: str
    memory: Memory
    seccomp: Seccomp
    cpus: Cpus
    sshPort: int
    mtu: int
    network: str | None
    address: str | None
    control: ControlSpecJson
    interfaces: list[InterfaceJson]
    uplink: str | None
    uplinkProxy: UplinkProxy
    uplinkNetwork: UplinkNetworkJson
    router: bool
    inbound: list[forward.InboundJson]
    boot: BootJson | None
    nested: bool
    display: DisplayJson | None
    storePaths: NotRequired[str]
    lateClosure: NotRequired[str]
    nixStore: NotRequired[str]
    systems: NotRequired[SystemsJson]
    launch: NotRequired[LaunchSpecJson]
    uplinkAddress: NotRequired[str]
    networkPorts: NotRequired[list[int]]
    requires: NotRequired[list[str]]


@closed
class SystemsJson(TypedDict):
    """`Systems` as Nix wrote it."""

    toplevel: str
    configurations: dict[str, str]


@dataclass(frozen=True)
class Systems:
    """The NixOS systems a guest has: the one it boots, and the ones it can
    switch to. Empty for a guest with no NixOS, such as a cloud image."""

    toplevel: str | None = None
    """The system this guest boots."""
    configurations: Mapping[str, str] = field(default_factory=dict)
    """`vivarium.configurations`: each name and its system, which the
    guest's Nix database already holds."""

    @classmethod
    def from_json(cls, data: SystemsJson) -> Systems:
        return cls(toplevel=data["toplevel"], configurations=dict(data["configurations"]))


@closed
class LaunchSpecJson(TypedDict):
    """`LaunchSpec` as Nix wrote it."""

    backend: str
    config: dict[str, Any]


@dataclass(frozen=True)
class LaunchSpec:
    """`vivarium.launch`: how a backend starts the guest.

    The backend's own module writes it. `config` is the backend's to read
    and to validate; the core only carries it.
    """

    backend: str
    config: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_json(cls, data: LaunchSpecJson) -> LaunchSpec:
        return cls(backend=data["backend"], config=dict(data["config"]))


@dataclass(frozen=True)
class Interface:
    """One of `vivarium.interfaces`: a link to a segment, named in the guest."""

    name: str
    nic: int
    segment: str
    addresses: tuple[str, ...]
    mac: str
    mtu: int
    """Its segment's `mtu`, else the guest's `vivarium.mtu`."""

    @classmethod
    def from_json(cls, data: InterfaceJson) -> Interface:
        return cls(
            name=data["name"],
            nic=data["nic"],
            segment=data["segment"],
            addresses=tuple(data["addresses"]),
            mac=data["mac"],
            mtu=data["mtu"],
        )


@dataclass(frozen=True)
class MachineSpec:
    """What Nix knows about a guest; see ``mkTest`` in lib.nix."""

    name: str
    backend: BackendName
    kind: str
    """`vivarium.kind`: `nixos`, or the guest kind it imports from `vivarium.kinds`."""
    index: int
    image: Path
    memory: str
    """Resolved: `vivarium.memory` against the host's available memory at start."""
    seccomp: Seccomp
    cpus: int
    """Resolved: `vivarium.cpus` against the host's cores at start."""
    ssh_port: int
    mtu: int
    network: str | None
    address: str | None
    control: ControlSpec
    """`vivarium.control`: the agent, a console dialect, or nothing."""
    interfaces: tuple[Interface, ...]
    """The guest's segment links, in NIC order."""
    uplink: str | None
    """The segment the uplink (`vec0`, NIC 0) joins: the router's port for
    this guest, or an out-of-band management network a peer serves
    (nixidae/nixidae#958). `None` is the guest's own relay or gvproxy."""
    uplink_proxy: UplinkProxy
    uplink_network: UplinkNetwork
    """The addresses on `vec0`, from nix/uplink.nix."""
    router: bool
    """The run's router guest (`vivarium.router.enable`): no `vec0`, and
    vsock to an egress on the host instead (nixidae/nixidae#1404)."""
    inbound: tuple[forward.Rule, ...]
    """`vivarium.inbound`: the host-side port forwards.  Addresses in these
    are still None until :func:`vivarium_runner.forward.resolve` has run over
    every machine in the run at once, which `vivarium run` does before it
    boots any."""
    boot: Boot | None
    """`vivarium.boot`. None for a guest that names nothing to boot, which
    every backend refuses."""
    nested: bool
    """`vivarium.nestedVirtualization`."""
    display: Display | None
    """`vivarium.display`, when enabled."""
    store: str = "/nix"
    """The directory a guest gets as its `/nix`. The runner points it at
    the guest's store view when the spec names `storePaths`."""
    store_paths: Path | None = None
    """A closureInfo's `store-paths`: the guest's closure, and all its view
    holds."""
    store_allowlist: Path | None = None
    """The paths a krun guest sees of the whole host store. The runner sets
    it when the spec names `storePaths`."""
    late_closure: Path | None = None
    """A closureInfo of `vivarium.lateClosure`, which the runner adds to the
    view and registers once the guest is up."""
    nix_store: str | None = None
    """The `nix-store` in the guest that registers a closure after boot.
    `None` is a guest with no Nix database: a path reaches its store view
    only."""
    systems: Systems = field(default_factory=Systems)
    launch: LaunchSpec | None = None
    """`vivarium.launch`, for a backend that reads its tools from there.
    The backend checks it when the `Machine` is made."""
    uplink_address: str | None = None
    """A guest behind the router: its address there, which the router's
    egress forwards to."""
    network_ports: tuple[int, ...] = ()
    """The router: the vsock port of each of its networks' relays to the
    egress (`vivarium.router.networkPorts`, nixidae/nixidae#1523)."""
    requires: tuple[str, ...] = ()
    """`vivarium.requires`: the guests this one cannot boot without. The
    session boots them first; the runner does nothing with it."""

    @property
    def vsock(self) -> bool:
        """No `vec0` from the host: the guest's relay reaches an egress over
        vsock. The router does it for its guests; a `vsock` guest for itself,
        unless its uplink joins a segment."""
        return self.router or (self.uplink_proxy == UplinkProxy.VSOCK and self.uplink is None)

    @property
    def forwards_through_egress(self) -> bool:
        """An egress binds this guest's forwards: its router's, or its own as a
        `vsock` guest. The router has none of its own (modules/router.nix)."""
        return self.uplink_address is not None or (self.vsock and not self.router)

    @classmethod
    def from_json(cls, data: MachineSpecJson) -> MachineSpec:
        store_paths = data.get("storePaths")
        late_closure = data.get("lateClosure")
        systems = data.get("systems")
        launch = data.get("launch")
        return cls(
            name=data["name"],
            backend=BackendName(data["backend"]),
            kind=data["kind"],
            index=data["index"],
            image=Path(data["image"]),
            memory=resolve_memory(data["memory"]),
            seccomp=Seccomp(data["seccomp"]),
            cpus=resolve_cpus(data["cpus"], host_cpus()),
            ssh_port=data["sshPort"],
            mtu=data["mtu"],
            network=data["network"],
            address=data["address"],
            control=ControlSpec.from_json(data["control"]),
            interfaces=tuple(Interface.from_json(one) for one in data["interfaces"]),
            uplink=data["uplink"],
            uplink_proxy=UplinkProxy(data["uplinkProxy"]),
            uplink_network=UplinkNetwork.from_json(data["uplinkNetwork"]),
            router=data["router"],
            inbound=tuple(forward.Rule.from_json(entry) for entry in data["inbound"]),
            boot=Boot.from_json(boot) if (boot := data["boot"]) is not None else None,
            nested=data["nested"],
            display=Display.from_json(shown) if (shown := data["display"]) is not None else None,
            store_paths=Path(store_paths) if store_paths is not None else None,
            late_closure=Path(late_closure) if late_closure is not None else None,
            nix_store=data.get("nixStore"),
            requires=tuple(data.get("requires", ())),
            systems=Systems.from_json(systems) if systems is not None else Systems(),
            launch=LaunchSpec.from_json(launch) if launch is not None else None,
            uplink_address=data.get("uplinkAddress"),
            network_ports=tuple(data.get("networkPorts", ())),
        )

    @property
    def ip(self) -> str | None:
        """The ``vec1`` address without its prefix length."""
        return self.address.split("/")[0] if self.address else None

    def mac(self, nic: int) -> str:
        """This guest's address on ``vecN``.

        It carries the machine's index, because two guests on one segment
        sharing a MAC is not a segment.  ``modules/qemu.nix`` matches these
        to name the interfaces, so the two must agree.
        """
        return f"52:54:00:12:{nic:02x}:{self.index:02x}"
