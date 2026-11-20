"""The closed value sets of a guest's spec, and the mark that makes a spec shape strict.

The runner reads a spec as `TypedDict`s (`MachineSpecJson` and the shapes it
holds). `vivarium.spec` validates them with pydantic before the runner sees
them; the runner and the guest agent run without pydantic.
"""

from __future__ import annotations

from enum import StrEnum


class BackendName(StrEnum):
    """`vivarium.backend`: the Nix option's enum."""

    UML = "uml"
    QEMU = "qemu"
    KRUN = "krun"
    CONTAINER = "container"


class Seccomp(StrEnum):
    """`vivarium.seccomp`."""

    AUTO = "auto"
    ON = "on"
    OFF = "off"


class UplinkProxy(StrEnum):
    """`vivarium.uplink.proxy`: what serves the guest's uplink."""

    ROUTER = "router"
    VSOCK = "vsock"


_PYDANTIC_CONFIG = "__pydantic_config__"


def closed[T: type](shape: T) -> T:
    """Make pydantic refuse a key that the `TypedDict` *shape* does not declare.

    The same as pydantic's `with_config(ConfigDict(extra="forbid"))`, without
    the import. A newer `lib.nix` that writes a field this runner does not
    know then fails with the field's path, before any guest boots.
    """
    setattr(shape, _PYDANTIC_CONFIG, {"extra": "forbid"})
    return shape
