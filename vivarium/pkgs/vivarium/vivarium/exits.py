"""The exit statuses of `vivarium`, the runner, `ctl` and `monitor`: one table.

A run's verdict is 0 to 2, and 4 to 6 say the run waits for someone.
3 and 7 are about reaching a run, not about the run. A signal ends a
process with 128 plus its number, which is not in this table.
"""

from __future__ import annotations

from enum import IntEnum
from typing import Final


class Exit(IntEnum):
    PASSED = 0
    """The run passed, or `ctl` did what it asked."""

    FAILED = 1
    """A phase failed, or what `ctl` asked for ran and failed: an `exec`
    that raised, a `pytest` with a failed case."""

    NO_VERDICT = 2
    """The run ended with no verdict: a crash, or an evaluation, build or
    spec error before the first phase."""

    USAGE = 2
    """A command line the program refuses. argparse exits 2 for its own,
    so this is the same number as `NO_VERDICT` (nixidae/nixidae#1483)."""

    NO_RUN = 3
    """No run to reach: none started in time, none listens on the socket,
    or its stream ended before the verdict."""

    PAUSED = 4
    """The run is paused, and waits for `ctl continue` or `stop`."""

    STOPPED = 5
    """`ctl stop` ended the run: not a pass, and not a failure."""

    READY = 6
    """An environment (`--hold`) is ready, its guests up. Not 0: a watcher
    must not read guests that are still up as a verdict."""

    REFUSED = 7
    """The run refused the request: the wrong time for it, or something
    it does not have, such as a phase name or a guest."""

    @property
    def label(self) -> str:
        return LABELS[self]


LABELS: Final = {
    Exit.PASSED: "passed",
    Exit.FAILED: "failed",
    Exit.NO_VERDICT: "no verdict",
    Exit.NO_RUN: "no run",
    Exit.PAUSED: "paused",
    Exit.STOPPED: "stopped",
    Exit.READY: "ready",
    Exit.REFUSED: "refused",
}
"""How a help text names each status."""


def describe(*statuses: Exit) -> str:
    """`0 passed, 1 failed, ...`, for a help text: every status, or those named."""
    return ", ".join(f"{status.value} {status.label}" for status in statuses or tuple(Exit))
