"""A task that outlives the call that starts it.

A guest's console pump, a cloud instance's boot and an agent channel's
serve loop each start in one call and end in another, often in another
task: a boot step starts them and the teardown step stops them. A task
group entered by either call cannot own them, so the owner of the whole
run enters one (`Session.live`) and each such task starts in it
through `hold`. The group still ends with them: an unexpected error in
one cancels the run, and the group does not exit while one runs.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import anyio
from anyio.abc import TaskGroup


@dataclass(eq=False, slots=True)
class Held:
    """One task `hold` started: stop it, or wait for its end."""

    scope: anyio.CancelScope = field(default_factory=anyio.CancelScope)
    done: anyio.Event = field(default_factory=anyio.Event)

    async def wait(self) -> None:
        """Until the task has ended, by itself or by `stop`."""
        await self.done.wait()

    async def stop(self) -> None:
        """Cancel the task and wait until it has ended.

        Never from the task itself: it would wait for its own end.
        """
        self.scope.cancel()
        await self.done.wait()


def hold[*Ts](group: TaskGroup, func: Callable[[*Ts], Awaitable[object]], *args: *Ts, name: str | None = None) -> Held:
    """Start `func(*args)` in *group*, the owner's, and return what stops it.

    A stop before the task begins is kept: the task then never runs its body.
    """
    held = Held()

    async def run() -> None:
        try:
            # A scope cancelled before it is entered acts at the body's
            # first checkpoint, and the body's first step still runs.
            if not held.scope.cancel_called:
                with held.scope:
                    await func(*args)
        finally:
            held.done.set()

    group.start_soon(run, name=name)
    return held
