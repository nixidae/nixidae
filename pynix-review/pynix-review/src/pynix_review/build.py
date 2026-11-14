"""Build a bundle's changed derivations within a wall-clock budget.

Each derivation builds in its own call once its changed inputs have built,
shallowest and cheapest first, at most `jobs` at once. Through the `daemon`
store the daemon decides where each build runs. One call per derivation,
not one for the whole set: a batch call cut short answers for nothing,
including what it finished.

Two deadlines. Past `wall` no build starts. At `kill_after` a running
build's call is cancelled: huggorm wakes the thread blocked on the daemon
socket, and pynixd stops a build no client waits for (nixidae/nixidae#1219;
measured: a 60 s build cut at 5 s returned at 5.0 s, and the rerun built
it again). Nix's own `timeout` and `max-silent-time` can bound one build
besides; pynixd enforces them.
"""

from __future__ import annotations

import json
import re
import sys
import time
from dataclasses import dataclass, field
from functools import partial
from typing import TYPE_CHECKING, Final

import anyio
import huggorm
from pydantic import BaseModel, ConfigDict, Field

from .bundle import (
    DEPENDENCY_FAILED,
    OUT_OF_BUDGET,
    OVER_BUDGET,
    RAN,
    STOPPED,
    TIMED_OUT,
    UNCHANGED_INPUT_FAILED,
    to_dot,
    view_url,
)
from .comment import COMMENT_FILE, URL_FILE, comment
from .evaluate import error_line
from .plan import STORE_DIR, UNTIMED_SECONDS, cost, order

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Container, Sequence

    from .bundle import Bundle, Derivation

DAEMON: Final = "daemon"
WALL_SECONDS: Final = 3600
KILL_AFTER_SECONDS: Final = 5400
JOBS: Final = 8
REPORT_FILE: Final = "built.json"
SCHEMA_FILE: Final = "built.schema.json"
DOT_FILE: Final = "built.dot"
TIMEOUT_SETTING: Final = "timeout"
"""Not `build-timeout`: an untrusted client may set only the primary name (`daemon.cc:297`)."""
SILENT_SETTING: Final = "max-silent-time"
LOG_LINES: Final = 60
LOG_QUOTE: Final = re.compile(r"\s*Last \d+ log lines:")
FAILED_BUILDER: Final = re.compile(r"Cannot build '/nix/store/(?P<drv>[^'/]+\.drv)'\.\s+Reason: (?P<reason>[^\n]*)")
HELD_BACK: Final = re.compile(r"\d+ dependenc(y|ies) failed")
TIMEOUT_MESSAGE: Final = re.compile(r"^error: timed out after \d+ seconds")
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
    log_tail: str | None = None
    outputs: dict[str, str] = field(default_factory=dict[str, str])


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
    """The last error paragraph on one line, on failure."""
    log_tail: str | None
    """The last `LOG_LINES` lines of the builder's log, when it ran and failed and the store kept its log."""
    outputs: dict[str, str]
    """Each output's store path, on success. Only a `built` one is this run's own to delete:
    an `already-valid` or `substituted` one was there, or in a cache, before."""


class UnchangedFailure(_Model):
    """A derivation the change does not touch whose own builder failed: why
    `unchanged-input-failed` results failed."""

    drv: str
    message: str
    log_tail: str | None


class Report(_Model):
    budget: int
    """Wall seconds allowed to start builds in."""
    kill_after: int
    """Wall seconds after which running builds are stopped."""
    build_timeout: int
    """Seconds one build may run, Nix's `timeout`; 0: the daemon's own."""
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
    """Not started in time, held back by such an input, `stopped` or `timed_out`."""
    stopped: int
    """Of `out_of_budget`, builds still running at `kill_after`."""
    timed_out: int
    """Of `out_of_budget`, builds `build_timeout` or `max-silent-time` stopped."""
    unchanged_failures: list[UnchangedFailure] = Field(default_factory=list[UnchangedFailure])
    """Found in Nix's error messages during the run: the derivations behind
    `unchanged_input_failed`, which can be several inputs deep."""
    unchanged_timeouts: int = 0
    """Builds outside the bundle that `build_timeout` or `max-silent-time`
    stopped: their dependents are `unchanged-input-failed` too, though
    nothing failed. Nix's message names no derivation ("timed out after 5
    seconds", upstream's `goal.cc` as pynixd's), so they are counted, not
    named. A bundle build that times out answers in its result and logs no
    such message (measured)."""


def unchanged_failure(text: str, changed: Container[str]) -> tuple[str, str] | None:
    """`(drv, message)` when `text` is Nix's error for a builder that failed
    in a derivation outside `changed`; not for one that a failed dependency
    held back, which names no cause."""
    found = FAILED_BUILDER.search(text)
    if found is None or found["drv"] in changed or HELD_BACK.match(found["reason"]):
        return None
    return found["drv"], build_message(text, logged=True)


def failure_status(nix_status: str) -> str:
    """The status word for a build Nix reports as failed."""
    return UNCHANGED_INPUT_FAILED if nix_status == DEPENDENCY_FAILED else nix_status


def held_back(drv: str, inputs: Sequence[Outcome]) -> Outcome | None:
    """`drv`'s outcome with no build, from its changed inputs' outcomes;
    None when they all succeeded."""
    failed = [o for o in inputs if not o.ok and o.status not in OVER_BUDGET]
    if failed and all(o.status == UNCHANGED_INPUT_FAILED for o in failed):
        return Outcome(drv, UNCHANGED_INPUT_FAILED, False, 0, 0, "a changed input failed on an unchanged input")
    if failed:
        return Outcome(drv, DEPENDENCY_FAILED, False, 0, 0, "a changed input failed")
    if any(o.status in OVER_BUDGET for o in inputs):
        return Outcome(drv, OUT_OF_BUDGET, False, 0, 0, "a changed input ran out of budget")
    return None


async def schedule(
    planned: Sequence[Derivation],
    build_one: Callable[[str], Awaitable[Outcome]],
    *,
    wall: float,
    jobs: int,
    kill_after: float | None = None,
) -> list[Outcome]:
    """Run `build_one` for each of `planned` after its changed inputs, at
    most `jobs` at once, starting none after `wall` seconds and cancelling
    any still running at `kill_after` (None: none). Answers in the order
    of `planned`."""
    begun = anyio.current_time()
    deadline = begun + wall
    kill = None if kill_after is None else begun + kill_after
    limiter = anyio.CapacityLimiter(jobs)
    finished = {d.drv: anyio.Event() for d in planned}
    outcomes: dict[str, Outcome] = {}

    async def one(d: Derivation) -> None:
        try:
            for i in d.changed_inputs:
                if i in finished:
                    await finished[i].wait()
            outcome = held_back(d.drv, [outcomes[i] for i in d.changed_inputs if i in outcomes])
            started = 0
            if outcome is None:
                async with limiter:
                    if anyio.current_time() >= deadline:
                        outcome = Outcome(d.drv, OUT_OF_BUDGET, False, 0, 0, f"not started within {wall:g} s")
                    else:
                        started = int(time.time())
                        with anyio.move_on_after(None if kill is None else kill - anyio.current_time()):
                            outcome = await build_one(d.drv)
            outcomes[d.drv] = outcome or Outcome(
                d.drv, STOPPED, False, started, int(time.time()), f"still running at {kill_after:g} s"
            )
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
    kill_after: int = KILL_AFTER_SECONDS,
    build_timeout: int = 0,
    unchanged_failures: Sequence[UnchangedFailure] = (),
    unchanged_timeouts: int = 0,
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
            log_tail=o.log_tail,
            outputs=o.outputs,
        )
        for o in outcomes
    ]
    timed = [r for r in results if r.ok and r.seconds is not None]
    return Report(
        budget=budget,
        kill_after=kill_after,
        build_timeout=build_timeout,
        estimate=sum(cost(d, untimed_seconds) for d in bundle.derivations),
        results=results,
        built=sum(1 for r in results if r.ok and r.status == RAN),
        built_seconds=sum(r.seconds or 0 for r in timed),
        built_estimate=sum(r.estimate or 0 for r in timed),
        wall_seconds=round(wall_seconds, 3),
        failed=sum(1 for r in results if not r.ok and r.status not in OVER_BUDGET),
        unchanged_input_failed=sum(1 for r in results if r.status == UNCHANGED_INPUT_FAILED),
        out_of_budget=sum(1 for r in results if r.status in OVER_BUDGET),
        stopped=sum(1 for r in results if r.status == STOPPED),
        timed_out=sum(1 for r in results if r.status == TIMED_OUT),
        unchanged_failures=list(unchanged_failures),
        unchanged_timeouts=unchanged_timeouts,
    )


async def write(made: Report, source: Bundle, directory: anyio.Path, view: str | None = None) -> list[anyio.Path]:
    """`built.json`, its schema, the changed graph coloured by what the build did, and `built.md`.
    `view` replaces the link that carries the DOT itself."""
    await directory.mkdir(parents=True, exist_ok=True)
    dot = to_dot(source, made)
    files = {
        REPORT_FILE: made.model_dump_json(indent=2) + "\n",
        SCHEMA_FILE: json.dumps(Report.model_json_schema(), indent=2) + "\n",
        DOT_FILE: dot,
        URL_FILE: (link := view or view_url(dot)) + "\n",
        COMMENT_FILE: comment(made, source, link),
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


def tail(log: str, lines: int = LOG_LINES) -> str:
    """The last `lines` lines of a build log, without colour codes."""
    return "\n".join(huggorm.filter_ansi_escapes(log, filter_all=True).splitlines()[-lines:])


def build_message(error: str, *, logged: bool) -> str:
    """The error on one line. With the log at hand, without the log lines
    libstore quotes after the reason: `error_line` would clip them anyway."""
    return error_line(LOG_QUOTE.split(error, maxsplit=1)[0] if logged else error)


async def _log_tail(store: huggorm.AsyncStore, drv: str) -> str | None:
    log = await store.get_build_log(await store.parse_store_path(f"{STORE_DIR}/{drv}"))
    return None if log is None else tail(log)


async def _build_one(store: huggorm.AsyncStore, drv: str) -> Outcome:
    target = await store.parse_derived_path(f"{STORE_DIR}/{drv}^*")
    [result] = await store.build_paths_with_results([target])
    success, error = result.success(), result.error()
    status = str(success.status()) if success is not None else failure_status(str(error.status if error else "unknown"))
    path = await store.parse_store_path(f"{STORE_DIR}/{drv}")
    outputs: dict[str, str] = {}
    log_tail = None
    if success is not None:
        outputs = {
            name: await store.print_store_path(p) for name, p in (await store.query_derivation_output_map(path)).items()
        }
    elif status != UNCHANGED_INPUT_FAILED:
        log_tail = await _log_tail(store, drv)
    return Outcome(
        drv=drv,
        status=status,
        ok=success is not None,
        start=result.start_time(),
        stop=result.stop_time(),
        message=build_message(str(error), logged=log_tail is not None) if error is not None else None,
        log_tail=log_tail,
        outputs=outputs,
    )


async def build(  # noqa: PLR0913 -- keyword-only, one per CLI option
    bundle: Bundle,
    *,
    wall: int = WALL_SECONDS,
    kill_after: int = KILL_AFTER_SECONDS,
    jobs: int = JOBS,
    untimed_seconds: int = UNTIMED_SECONDS,
    store_url: str = DAEMON,
    build_logs: bool = False,
    build_timeout: int = 0,
    max_silent_time: int = 0,
) -> Report:
    """Build `bundle`'s changed derivations. Each call holds one of
    huggorm's pool threads, so the pool needs `jobs` of them. stderr gets
    Nix's warnings and errors; with `build_logs` also its messages at
    info and each builder's output.

    `build_timeout` and `max_silent_time` (0: the daemon's own) set Nix's
    settings for the process: the session sends them at connect."""

    changed = {d.drv for d in bundle.derivations}
    unchanged: dict[str, str] = {}
    """Nix names a failed builder once per derivation waiting on it: one entry each."""
    timeouts: list[str] = []

    def write_log(record: huggorm.LogRecord) -> None:
        if (line := log_line(record, build_logs=build_logs)) is not None:
            text = huggorm.filter_ansi_escapes(line, filter_all=True)
            sys.stderr.write(text + "\n")
            if record.action() != "msg":
                return
            if (found := unchanged_failure(text, changed)) is not None:
                unchanged.setdefault(*found)
            elif TIMEOUT_MESSAGE.match(text):
                timeouts.append(text)

    for name, seconds in ((TIMEOUT_SETTING, build_timeout), (SILENT_SETTING, max_silent_time)):
        if seconds:
            huggorm.set_setting(name, str(seconds))
    start = time.monotonic()
    level = huggorm.Verbosity.INFO if build_logs else huggorm.Verbosity.WARN
    async with huggorm.AsyncSession(store_url) as session, session.forward_process(write_log, level=int(level)):
        store = session.store()
        outcomes = await schedule(
            order(bundle, untimed_seconds), partial(_build_one, store), wall=wall, jobs=jobs, kill_after=kill_after
        )
        culprits = [
            UnchangedFailure(drv=drv, message=message, log_tail=await _log_tail(store, drv))
            for drv, message in unchanged.items()
        ]
    return report(
        bundle,
        outcomes,
        budget=wall,
        wall_seconds=time.monotonic() - start,
        untimed_seconds=untimed_seconds,
        kill_after=kill_after,
        build_timeout=build_timeout,
        unchanged_failures=culprits,
        unchanged_timeouts=len(timeouts),
    )
