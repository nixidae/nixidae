"""Booted guests that tests take and give back. nixidae/nixidae#1413.

A test asks for a guest of a template. A `clean` one no test touched comes
from the spares, which boot in the background through the run's cloud API
(`Cloud.launch`), so the test waits for no boot while a spare is ready. A
`dirty` request takes a guest an earlier test gave back dirty, and a spare
when none is. A clean release terminates the guest; a dirty one keeps it
for the next dirty request.

Every pool instance carries `TAG`. pynixd's vivarium provider lists the
cloud with its own owner tag, so it never sees one.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from vivarium_runner.held import hold

from .cloud import State

if TYPE_CHECKING:
    from .cloud import Cloud, Instance

TAG: Final = "vivarium.pool"
"""The tag every pool instance carries; its value is the template."""

BOOT_WAIT: Final = 300.0
"""Seconds a request waits for its guest to boot."""


class PoolError(RuntimeError):
    """A request the pool cannot serve."""


@dataclass
class Pool:
    cloud: Cloud
    spares: int = 1
    """How many clean guests of each template boot ahead of a request."""
    _ready: dict[str, deque[Instance]] = field(default_factory=dict)
    _dirty: dict[str, deque[str]] = field(default_factory=dict)
    _taken: dict[str, str] = field(default_factory=dict)
    """Each guest a test holds, and its template."""

    async def acquire(self, template: str, *, clean: bool = True, spares: int | None = None) -> str:
        """A guest of *template* for one test: its name, once it runs."""
        if template not in self.cloud.session.templates:
            known = ", ".join(self.cloud.session.templates) or "none"
            raise PoolError(f"no template {template!r}; the run has {known}")
        if spares is not None:
            self.spares = spares
        name = self._used(template) if not clean else None
        if name is None:
            ready = self._ready.setdefault(template, deque())
            instance = ready.popleft() if ready else self._launch(template)
            self._refill(template)
            await self.cloud.wait_running(instance.id, timeout=BOOT_WAIT)
            name = instance.id
        self._taken[name] = template
        return name

    async def release(self, name: str, *, dirty: bool = False) -> None:
        """Give *name* back: kept for a dirty request, or terminated."""
        template = self._taken.pop(name, None)
        if template is None:
            raise PoolError(f"{name} is no guest the pool handed out")
        if dirty:
            self._dirty.setdefault(template, deque()).append(name)
            return
        # In the background: a poweroff is not the test's time.
        hold(self.cloud.session.tasks, self.cloud.terminate, name)

    def _used(self, template: str) -> str | None:
        """A guest given back dirty that still runs; one that stopped meanwhile is dropped."""
        dirty = self._dirty.get(template, deque())
        while dirty:
            name = dirty.popleft()
            if self.cloud.instances[name].state is State.RUNNING:
                return name
        return None

    def _launch(self, template: str) -> Instance:
        return self.cloud.launch(template, {TAG: template}, None)

    def _refill(self, template: str) -> None:
        ready = self._ready.setdefault(template, deque())
        while len(ready) < self.spares:
            ready.append(self._launch(template))
