"""Build a bundle's changed derivations within a wall-clock budget.

Each derivation builds in its own call once its changed inputs have built,
shallowest and cheapest first, at most `jobs` at once. Through the `daemon`
store the daemon decides where each build runs. One call per derivation,
not one for the whole set: a batch call cut short answers for nothing,
including what it finished.

The deadline is soft. Past it no build starts, and a running one finishes:
a cancelled call waits for its build anyway (nixidae/nixidae#1219), and
pynixd keeps building after its client goes.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Final

import anyio
import huggorm
from pydantic import BaseModel, ConfigDict

from .bundle import DEPENDENCY_FAILED, OUT_OF_BUDGET, UNCHANGED_INPUT_FAILED, to_dot, view_url
from .evaluate import error_line
from .plan import STORE_DIR, UNTIMED_SECONDS, cost, order

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from .bundle import Bundle, Derivation

DAEMON: Final = "daemon"
WALL_SECONDS: Final = 3600
JOBS: Final = 8
REPORT_FILE: Final = "built.json"
DOT_FILE: Final = "built.dot"
URL_FILE: Final = "built.url"
RAN: Final = "built"
"""The success word for a builder that ran; the other success words ran none."""
BUILD_LOG_RESULTS: Final = frozenset({huggorm.ResultType.BUILD_LOG_LINE, huggorm.ResultType.POST_BUILD_LOG_LINE})
"""Unforwarded, huggorm's fallback logger prints these at chatty: 8694
lines on stderr for the libpng plan."""


@dataclass(frozen=True, slots=True)
class Outcome:
    drv: str
    status: str
    """A `BuildSuccessStatus` word on success, a `BuildFailureStatus` word on
    failure, or `DEPENDENCY_FAILED` / `OUT_OF_BUDGET` for no build."""
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
    """Wall time of the builder, when one ran and the store reported its
    times. Nix 2.35's `buildDerivation` answers 0 for both
    (nixidae/nixidae#1217), and then this is None."""
    estimate: int | None
    """The bundle's estimate for this derivation."""
    message: str | None
    """The first line of the error, on failure."""


class Report(_Model):
    budget: int
    """Wall seconds allowed to start builds in."""
    estimate: int
    """Every changed derivation, untimed ones at the planner's guess."""
    results: list[Built]
    built: int
    """Derivations a builder ran for, successfully."""
    built_seconds: int
    """The sum of `seconds`: builder time, not wall time, when builds ran in parallel."""
    built_estimate: int
    """The estimates of the derivations `built_seconds` counts."""
    wall_seconds: float
    failed: int
    """Failed builds, and derivations a failed input held back."""
    unchanged_input_failed: int
    """Of `failed`, those an input the change does not touch failed."""
    out_of_budget: int


def failure_status(nix_status: str) -> str:
    """The status word for a build Nix reports as failed."""
    return UNCHANGED_INPUT_FAILED if nix_status == DEPENDENCY_FAILED else nix_status


def held_back(drv: str, inputs: Sequence[Outcome]) -> Outcome | None:
    """`drv`'s outcome with no build, from its changed inputs' outcomes;
    None when they all succeeded."""
    failed = [o for o in inputs if not o.ok and o.status != OUT_OF_BUDGET]
    if failed and all(o.status == UNCHANGED_INPUT_FAILED for o in failed):
        return Outcome(drv, UNCHANGED_INPUT_FAILED, False, 0, 0, "a changed input failed on an unchanged input")
    if failed:
        return Outcome(drv, DEPENDENCY_FAILED, False, 0, 0, "a changed input failed")
    if any(o.status == OUT_OF_BUDGET for o in inputs):
        return Outcome(drv, OUT_OF_BUDGET, False, 0, 0, "a changed input ran out of budget")
    return None


async def schedule(
    planned: Sequence[Derivation],
    build_one: Callable[[str], Awaitable[Outcome]],
    *,
    wall: float,
    jobs: int,
) -> list[Outcome]:
    """Run `build_one` for each of `planned` after its changed inputs, at
    most `jobs` at once, starting none after `wall` seconds. Answers in
    the order of `planned`."""
    deadline = anyio.current_time() + wall
    limiter = anyio.CapacityLimiter(jobs)
    finished = {d.drv: anyio.Event() for d in planned}
    outcomes: dict[str, Outcome] = {}

    async def one(d: Derivation) -> None:
        try:
            for i in d.changed_inputs:
                if i in finished:
                    await finished[i].wait()
            outcome = held_back(d.drv, [outcomes[i] for i in d.changed_inputs if i in outcomes])
            if outcome is None:
                async with limiter:
                    if anyio.current_time() >= deadline:
                        outcome = Outcome(d.drv, OUT_OF_BUDGET, False, 0, 0, f"not started within {wall:g} s")
                    else:
                        outcome = await build_one(d.drv)
            outcomes[d.drv] = outcome
        finally:
            finished[d.drv].set()

    async with anyio.create_task_group() as tg:
        for d in planned:
            tg.start_soon(one, d)
    return [outcomes[d.drv] for d in planned]


def report(
    bundle: Bundle,
    outcomes: Sequence[Outcome],
    *,
    budget: int,
    wall_seconds: float,
    untimed_seconds: int = UNTIMED_SECONDS,
) -> Report:
    by_drv = {d.drv: d for d in bundle.derivations}
    results = [
        Built(
            drv=o.drv,
            pname=by_drv[o.drv].pname if o.drv in by_drv else o.drv,
            status=o.status,
            ok=o.ok,
            seconds=o.stop - o.start if (o.status == RAN or not o.ok) and 0 < o.start <= o.stop else None,
            estimate=t.estimate if (t := by_drv[o.drv].time if o.drv in by_drv else None) else None,
            message=o.message,
        )
        for o in outcomes
    ]
    timed = [r for r in results if r.ok and r.seconds is not None]
    return Report(
        budget=budget,
        estimate=sum(cost(d, untimed_seconds) for d in bundle.derivations),
        results=results,
        built=sum(1 for r in results if r.ok and r.status == RAN),
        built_seconds=sum(r.seconds or 0 for r in timed),
        built_estimate=sum(r.estimate or 0 for r in timed),
        wall_seconds=round(wall_seconds, 3),
        failed=sum(1 for r in results if not r.ok and r.status != OUT_OF_BUDGET),
        unchanged_input_failed=sum(1 for r in results if r.status == UNCHANGED_INPUT_FAILED),
        out_of_budget=sum(1 for r in results if r.status == OUT_OF_BUDGET),
    )


async def write(made: Report, source: Bundle, directory: anyio.Path, view: str | None = None) -> list[anyio.Path]:
    """`built.json`, and the changed graph coloured by what the build did.
    `view` replaces the link that carries the DOT itself."""
    await directory.mkdir(parents=True, exist_ok=True)
    dot = to_dot(source, made)
    files = {
        REPORT_FILE: made.model_dump_json(indent=2) + "\n",
        DOT_FILE: dot,
        URL_FILE: (view or view_url(dot)) + "\n",
    }
    written = []
    for name, text in files.items():
        await (directory / name).write_text(text)
        written.append(directory / name)
    return written


def log_line(record: huggorm.LogRecord, *, build_logs: bool) -> str | None:
    """The stderr line for `record`, or None: a message, and a build's own
    output only with `build_logs`."""
    match record.action():
        case "msg":
            info = record.info()
            return record.text() if info is None else info.msg()
        case "result" if build_logs and record.type() in BUILD_LOG_RESULTS:
            texts = [f.text() for f in record.fields() if not f.is_int()]
            return texts[-1] if texts else None
        case _:
            return None


async def _build_one(store: huggorm.AsyncStore, drv: str) -> Outcome:
    target = await store.parse_derived_path(f"{STORE_DIR}/{drv}^*")
    [result] = await store.build_paths_with_results([target])
    success, error = result.success(), result.error()
    return Outcome(
        drv=drv,
        status=str(success.status())
        if success is not None
        else failure_status(str(error.status if error else "unknown")),
        ok=success is not None,
        start=result.start_time(),
        stop=result.stop_time(),
        message=error_line(str(error)) if error is not None else None,
    )


async def build(  # noqa: PLR0913 -- keyword-only, one per CLI option
    bundle: Bundle,
    *,
    wall: int = WALL_SECONDS,
    jobs: int = JOBS,
    untimed_seconds: int = UNTIMED_SECONDS,
    store_url: str = DAEMON,
    build_logs: bool = False,
) -> Report:
    """Build `bundle`'s changed derivations. Each call holds one of
    huggorm's pool threads, so the pool needs `jobs` of them. stderr gets
    Nix's warnings and errors; with `build_logs` also its messages at
    info and each builder's output."""

    def write_log(record: huggorm.LogRecord) -> None:
        if (line := log_line(record, build_logs=build_logs)) is not None:
            sys.stderr.write(huggorm.filter_ansi_escapes(line, filter_all=True) + "\n")

    start = time.monotonic()
    level = huggorm.Verbosity.INFO if build_logs else huggorm.Verbosity.WARN
    async with huggorm.AsyncSession(store_url) as session, session.forward_process(write_log, level=int(level)):
        store = session.store()
        outcomes = await schedule(order(bundle, untimed_seconds), partial(_build_one, store), wall=wall, jobs=jobs)
    return report(bundle, outcomes, budget=wall, wall_seconds=time.monotonic() - start, untimed_seconds=untimed_seconds)
