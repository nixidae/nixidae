"""What a guest is made of, for each kind of machine.

:class:`Machine` owns a guest's lifetime and its console, and everything it
does above this package is the same whichever backend it has.  A backend's
whole job is to answer one question: what do I exec, with which fds, and
what else has to be running alongside it.

The host-side plumbing is shared, not reimplemented.  Every backend takes:

    the agent socketpair    a fd carrying arpyc, raw
    the segment fd          a fd from :mod:`vivarium_runner.net`, raw frames
    the egress              the uplink's host half, over vsock

UML takes the segment fd as ``vec1:transport=fd``; QEMU takes the same fd
as ``dgram,local.type=fd``.  Both are plain ``send``/``recv`` on the fd
with no framing, which is why one segment can hold guests of either kind.
"""

from __future__ import annotations

import functools
import importlib.metadata

from .base import (
    Backend as Backend,
    BackendError as BackendError,
    Launch as Launch,
    Stop as Stop,
)

ENTRY_POINTS = "vivarium.backends"
"""The entry point group a package names its backend classes in. The
runner's own four are there too, in its pyproject.toml, so a backend from
another package is found the same way and no registry here is edited."""


@functools.cache
def available() -> dict[str, Backend]:
    """Every installed backend, by the name a spec's ``backend`` gives."""
    found: dict[str, Backend] = {}
    for entry in importlib.metadata.entry_points(group=ENTRY_POINTS):
        backend: Backend = entry.load()()
        if backend.name != entry.name:
            raise BackendError(
                f"entry point {entry.name} = {entry.value} names a backend called {backend.name!r}; the names must match"
            )
        if entry.name in found:
            raise BackendError(f"two installed packages name a backend {entry.name!r}")
        found[entry.name] = backend
    return found


def get(name: str) -> Backend:
    """The backend called *name*, as the spec's ``backend`` field names it."""
    backends = available()
    try:
        return backends[name]
    except KeyError:
        raise ValueError(f"no backend {name!r}; have {', '.join(sorted(backends))}") from None
