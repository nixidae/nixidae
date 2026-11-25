"""The order of one run: boot, the phases, the pauses, the evidence, teardown.

`drive` is the one way in, for `vivarium-runner run` and the tests
alike. The rules it applies are `phases`' pure functions; the effects
are the `Session`'s.
"""

from __future__ import annotations

import contextlib
import os
from collections.abc import Collection
from pathlib import Path
from typing import Final

import anyio
from vivarium_runner import MachineError

from . import access, registry, remote, repl
from .control import READY, RUN_FILE, Controller, Origin, Subject, first_guest, run_file, vivarium_command
from .events import Kind, Level
from .feed import Feed
from .phases import PhaseState, launchable, ready
from .session import Session
from .spec import PhaseSpec

DRAIN: Final = 5.0
"""Seconds the run waits, after its verdict, for each watcher to have it."""


async def drive(
    session: Session,
    *,
    breaks: Collection[str] = (),
    break_on_failure: bool = False,
    break_on_start: bool = False,
    break_at_end: bool = False,
    hold: bool = False,
    interactive: bool = False,
    serial: bool = False,
    control: bool = True,
    origin: Origin | None = None,
    spec: Path | None = None,
    sequence: anyio.CancelScope | None = None,
) -> None:
    """Boot, run what is pending, write the evidence, put the guests down.

    Separate from `run` so it can be driven with something other than a
    command line -- which is what an MCP server does, and what the test
    for the teardown path does.

    The guests' journals stream beside it for the whole drive, a pause
    included: a guest left up after a failure keeps logging, and that is
    often what explains the failure. The control socket exists for the
    whole drive unless `control` is false, so `exec` reaches a run that
    never pauses.
    """
    wanted = control or breaks or break_on_failure or break_on_start or break_at_end or hold or interactive
    # `ctl stop` and *sequence*'s owner cancel the sequence and nothing
    # else: its shielded `finally` writes the evidence and puts the guests
    # down, and the socket stays up until each watcher has the verdict.
    if sequence is None:
        sequence = anyio.CancelScope()
    feed = Feed(session.out.name) if wanted else None
    if feed is not None:
        session.tap(feed)
    async with anyio.create_task_group() as group:
        controller = Controller(session, stop=sequence.cancel, origin=origin, feed=feed) if wanted else None
        group.start_soon(session.follow)  # type: ignore[call-arg]  # zuban-bug: start_soon counts defaulted parameters
        if controller is not None:
            await group.start(controller.serve)
            await group.start(remote.serve, session)
            if interactive:
                group.start_soon(repl.serve, controller)
        # Before any guest boots, after the socket it names exists, and
        # before the printed commands: a reader of those reads this next.
        about = run_file(
            vivarium_command(session),
            session.out.resolve(),
            os.getpid(),
            control=controller is not None,
            origin=origin,
            node=first_guest(session),
            test=Subject.of(session, spec),
        )
        out = anyio.Path(session.out)
        await out.mkdir(parents=True, exist_ok=True)
        await (out / RUN_FILE).write_text(about.to_json())
        if controller is not None:
            controller.announce()
        listed = registry.registered(session.out.resolve()) if controller is not None else contextlib.nullcontext()
        try:
            async with session.live():
                with listed, sequence:
                    await _sequence(
                        session,
                        controller,
                        set(breaks),
                        break_on_failure,
                        serial,
                        start=break_on_start,
                        end=break_at_end or interactive,
                        hold=hold,
                    )
        finally:
            if feed is not None:
                feed.close()
                # Bounded: a watcher that stopped reading must not hold the run up.
                with anyio.CancelScope(shield=True), anyio.move_on_after(DRAIN):
                    await feed.drained()
            group.cancel_scope.cancel()
            session.roots.close()


async def _sequence(
    session: Session,
    control: Controller | None,
    breaks: set[str],
    break_on_failure: bool,
    serial: bool = False,
    *,
    start: bool = False,
    end: bool = False,
    hold: bool = False,
) -> None:
    try:
        # Inside the `try`, not before it. `_start_all` lets every guest
        # settle before reporting, so a failed boot can leave others
        # running -- and outside this block nothing would ever stop them.
        selection = [p for p in session.spec.phases if session.state[p.name] is not PhaseState.DESELECTED]
        if len(selection) == len(session.spec.phases):
            # A full run boots every guest first: a phase may reach a peer
            # it names in neither `nodes` nor `requires`.
            await session.boot()
        elif start and control is not None:
            # Paused "with the guests up": those the selected phases need.
            await session.boot({name for phase in selection for name in session.needs(phase)})
        else:
            # Each phase boots its own guests as it starts.
            await session.boot(())
        if start and control is not None:
            await control.pause("before the first phase")
        await _schedule(session, control, breaks, break_on_failure, serial)
        if end and control is not None:
            await control.pause("after the last phase")
        # `continue` at ready runs what a `reload` added, then holds again;
        # only `stop` ends the hold.
        while hold and control is not None and session.passed and not control.ended:
            await control.pause(READY)
            if not control.ended:
                await _schedule(session, control, breaks, break_on_failure, serial)
    except MachineError as error:
        # A guest that would not boot. Every phase stays pending, so the
        # run fails on its own account below; this only keeps a traceback
        # about sockets out of the way of the message that matters.
        session.emit(Kind.ERROR, f"no guests: {error}", level=Level.ERROR)
        session._replay()
    finally:
        # So `events.jsonl` has the guests' last words before its verdict.
        # Shielded, so a ^C at a breakpoint still writes the evidence
        # and puts the guests down rather than leaving it to
        # `die_with_parent`.
        with anyio.CancelScope(shield=True):
            await session.drain()
            session.write_output()
            # With no control socket nobody reaches the guests any more,
            # and their evidence is drained: a poweroff only costs time.
            await session.teardown(kill=control is None)
            session.write_report()
            # A kubeconfig names guests that are gone, and in a sandboxed
            # check it would carry an admin key into the world-readable store.
            access.empty(session.out)


async def _schedule(
    session: Session,
    control: Controller | None,
    breaks: set[str],
    break_on_failure: bool,
    serial: bool,
) -> None:
    """Start every phase that may start, and again each time one ends.

    Not in waves: a wave waits for its slowest phase before the next
    starts, which gives the gain back on any graph deeper than one level.

    What is ready is asked again every time, never snapshotted. A phase
    that failed marks its dependents skipped *while this runs*, and a list
    taken before would still hold them -- which once ran `check` against a
    cluster that had already failed.

    **A pause waits for quiet.** A breakpoint or a failure stops new
    phases, and the pause begins when the running ones have ended. So
    paused always means nothing is running, and `run` or `pytest` by hand
    never races a phase. `exec` and `inject` may: the caller asked for it.
    """
    every = frozenset(machine["name"] for machine in session.spec.machines)
    running: dict[str, PhaseSpec] = {}
    failed: list[str] = []
    paused_before: set[str] = set()
    changed = anyio.Event()

    async def one(phase: PhaseSpec) -> None:
        try:
            if await session.run(phase) is PhaseState.FAILED:
                failed.append(phase.name)
        finally:
            del running[phase.name]
            changed.set()

    async with anyio.create_task_group() as group:
        while True:
            candidates = [p for p in ready(session.spec.phases, session.state) if p.name not in running]
            reason = None
            if control is not None:
                if break_on_failure and failed:
                    reason = f"after {', '.join(failed)} failed"
                elif stop := [p.name for p in candidates if p.name in breaks - paused_before]:
                    reason = f"before {stop[0]}"
            if reason is not None and control is not None:
                if not running:
                    if reason.startswith("before "):
                        name = reason.removeprefix("before ")
                        paused_before.add(name)
                        # A pause has its phase's guests up, which under
                        # `--only` nothing has booted yet.
                        try:
                            await session.ensure(session.needs(next(p for p in candidates if p.name == name)))
                        except MachineError as error:
                            # The phase fails on it when it runs.
                            session.emit(Kind.ERROR, f"before {name}: {error}", level=Level.ERROR, phase=name)
                    failed.clear()
                    # Asked again afterwards: a phase run by hand while
                    # paused changed what is ready.
                    await control.pause(reason)
                    continue
                todo = []
            else:
                todo = launchable(candidates, running.values(), every)
                if serial:
                    todo = todo[:1] if not running else []
            for phase in todo:
                running[phase.name] = phase
                group.start_soon(one, phase, name=f"phase {phase.name}")
            if not running:
                break
            await changed.wait()
            changed = anyio.Event()
    for name, state in session.state.items():
        if state is PhaseState.PENDING:
            # Only a spec not from Nix can do this: Nix asserts every
            # `after` names a phase.
            session.emit(
                Kind.ERROR,
                f"{name} never ran: something in its `after` never finished",
                level=Level.ERROR,
                phase=name,
            )
