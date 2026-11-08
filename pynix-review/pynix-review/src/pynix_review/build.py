"""Build a plan through a Nix store, and report on each derivation.

`build_paths_with_results` answers one result per target and raises for no
build failure, so one failed derivation leaves the rest of the plan going.
Through the `daemon` store the daemon decides where each build runs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import anyio
import huggorm
from pydantic import BaseModel, ConfigDict

from .bundle import to_dot, view_url

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .bundle import Bundle
    from .plan import Plan

DAEMON: Final = "daemon"
REPORT_FILE: Final = "built.json"
DOT_FILE: Final = "built.dot"
URL_FILE: Final = "built.url"
RAN: Final = "built"
"""The success word for a builder that ran; the other success words ran none."""


@dataclass(frozen=True, slots=True)
class Outcome:
    drv: str
    status: str
    """A `BuildSuccessStatus` word on success, a `BuildFailureStatus` word on failure."""
    ok: bool
    start: int
    stop: int
    message: str | None


class _Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", use_attribute_docstrings=True)


class Built(_Model):
    drv: str
    pname: str
    status: str
    ok: bool
    seconds: int | None
    """Wall time of the builder, when one ran and the store reported its times.
    pynixd reports 0 for both, so through it this is None."""
    estimate: int | None
    """The bundle's estimate for this derivation."""
    message: str | None
    """The first line of the error, on failure."""


class Report(_Model):
    budget: int
    estimate: int
    results: list[Built]
    built_seconds: int
    """The sum of `seconds`: builder time, not wall time, when builds ran in parallel."""
    wall_seconds: float
    failed: int


def report(plan: Plan, bundle: Bundle, outcomes: Sequence[Outcome], wall_seconds: float) -> Report:
    by_drv = {d.drv: d for d in bundle.derivations}
    results = [
        Built(
            drv=o.drv,
            pname=by_drv[o.drv].pname if o.drv in by_drv else o.drv,
            status=o.status,
            ok=o.ok,
            seconds=o.stop - o.start if o.ok and o.status == RAN and 0 < o.start <= o.stop else None,
            estimate=t.estimate if (t := by_drv[o.drv].time if o.drv in by_drv else None) else None,
            message=o.message,
        )
        for o in outcomes
    ]
    return Report(
        budget=plan.budget,
        estimate=plan.estimate,
        results=results,
        built_seconds=sum(r.seconds or 0 for r in results),
        wall_seconds=round(wall_seconds, 3),
        failed=sum(1 for r in results if not r.ok),
    )


async def write(made: Report, source: Bundle, directory: anyio.Path) -> list[anyio.Path]:
    """`built.json`, and the changed graph coloured by what the build did."""
    await directory.mkdir(parents=True, exist_ok=True)
    dot = to_dot(source, {r.drv: r for r in made.results})
    files = {REPORT_FILE: made.model_dump_json(indent=2) + "\n", DOT_FILE: dot, URL_FILE: view_url(dot) + "\n"}
    written = []
    for name, text in files.items():
        await (directory / name).write_text(text)
        written.append(directory / name)
    return written


def _base(installable: str) -> str:
    return installable.removesuffix("^*").rsplit("/", 1)[-1]


async def build(plan: Plan, bundle: Bundle, *, store_url: str = DAEMON) -> Report:
    start = time.monotonic()
    store = huggorm.AsyncStore(store_url)
    try:
        targets = [await store.parse_derived_path(i) for i in plan.installables]
        results = await store.build_paths_with_results(targets) if targets else []
    finally:
        await store.aclose()
    outcomes = []
    for installable, result in zip(plan.installables, results, strict=True):
        success, error = result.success(), result.error()
        outcomes.append(
            Outcome(
                drv=_base(installable),
                status=str(success.status()) if success is not None else str(error.status if error else "unknown"),
                ok=success is not None,
                start=result.start_time(),
                stop=result.stop_time(),
                message=str(error).splitlines()[0][:300] if error is not None else None,
            )
        )
    return report(plan, bundle, outcomes, time.monotonic() - start)
