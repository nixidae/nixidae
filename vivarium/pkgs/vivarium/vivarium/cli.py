"""`vivarium-runner`: the command line of one run, and `ctl`, `monitor`, `logs`.

Thin on purpose: arguments, sinks, signals and the exit code. The order of
a run is `driver.drive`, the rules are `phases`, and the session holds the
work, so an MCP server or a test drives the same run without this module.

**One program, run two ways.** A build runs it with `--out $out`, and a
developer runs the same binary with `--out somewhere`. Nothing branches
on which, so the two cannot drift -- and drift is where every asymmetry
in the old pair came from: no log by hand, no report by hand, a `$@` that
was documented and rejected.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import functools
import json
import os
import shlex
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Final

import anyio
from vivarium_runner.machine_spec import Cpus

from . import liveness, logs, monitor, namespace, places, registry, runroot
from .control import (
    ORIGIN,
    PARENT,
    RUN_FILE,
    SOCKET,
    Op,
    Origin,
    RunFile,
    WatchRefused,
    request,
    watch as watch_run,
)
from .driver import drive
from .events import Kind, Level
from .exits import Exit, describe
from .phases import END, FAILURE, START, Stop, breakpoints, summarise
from .session import PerGuest, Session, SessionError
from .sinks import Broadcast, ConsoleFiles, JsonLines, Junit, Log, Terminal
from .spec import Spec, SpecError

STOP_WAIT: Final = 300.0
"""Seconds `ctl stop` waits for the run to exit. Teardown powers each
guest off, then signals what is left, each step bounded."""


def _absolute(value: str) -> Path:
    """A path argument, made absolute against the directory vivarium started in.

    A guest's `/init` mounts `<out>/artifacts/<machine>` from another working
    directory, so a relative `--out` boots no guest. nixidae/nixidae#17.
    """
    return Path(value).absolute()


def _cpus_value(kind: type[int] | type[float]) -> Callable[[str], tuple[str | None, Cpus]]:
    """`N` for every guest, or `GUEST=N` for one; a scale for `float`."""

    def parsed(value: str) -> tuple[str | None, Cpus]:
        guest, _, number = value.rpartition("=")
        try:
            amount = kind(number)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{value}: {number!r} is not a number") from None
        if amount <= 0:
            raise argparse.ArgumentTypeError(f"{value}: {number} is not above zero")
        cpus: Cpus = {"scale": float(amount)} if kind is float else int(amount)
        return (guest or None, cpus)

    return parsed


def _kernel_value(value: str) -> tuple[str | None, Path]:
    """`PATH` for every guest, or `GUEST=PATH` for one. A guest's name has no
    `/`, so `=` in a path is a path."""
    guest, sep, path = value.partition("=")
    if not sep or "/" in guest:
        return (None, _absolute(value))
    return (guest, _absolute(path))


def _per_guest[T](given: list[tuple[str | None, T]], what: str) -> PerGuest[T] | None:
    every: T | None = None
    guests: dict[str, T] = {}
    for guest, value in given:
        if guest is None:
            if every is not None:
                raise SystemExit(f"vivarium: give the {what} of every guest once")
            every = value
        elif guest in guests:
            raise SystemExit(f"vivarium: give {guest}'s {what} once")
        else:
            guests[guest] = value
    return PerGuest(every=every, guests=guests) if given else None


def cpus_of(args: argparse.Namespace) -> PerGuest[Cpus] | None:
    """`--cpus` and `--cpu-scale`, each `N` for every guest or `GUEST=N`."""
    return _per_guest([*args.cpus, *args.cpu_scale], "processors")


def kernels_of(args: argparse.Namespace) -> PerGuest[Path] | None:
    """`--kernel`, `PATH` for every guest or `GUEST=PATH`."""
    return _per_guest(args.kernel, "kernel")


def parse(argv: list[str] | None = None) -> argparse.Namespace:
    """The command line, and whatever follows `--` for pytest.

    `vivarium run --spec s --out o -- -k hostname -x` hands `-k hostname -x`
    to every pytest phase. Split here rather than left to argparse, whose
    REMAINDER takes the first unknown flag as the start of it.
    """
    argv = sys.argv[1:] if argv is None else list(argv)
    extra: list[str] = []
    if "--" in argv:
        at = argv.index("--")
        argv, extra = argv[:at], argv[at + 1 :]
    parser = argparse.ArgumentParser(
        prog="vivarium",
        description="Run NixOS guests",
        epilog="Arguments after -- go to every pytest phase.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    running = sub.add_parser("run", help="boot the guests and run every phase")
    running.add_argument("--spec", type=_absolute, required=True, help="the spec Nix wrote")
    add_run_options(running.add_argument)
    running.set_defaults(handler=run_command)

    listing = sub.add_parser("phases", help="list the phases and exit")
    listing.add_argument("--spec", type=_absolute, required=True)
    listing.set_defaults(handler=functools.partial(anyio.run, phases))

    add_ctl(sub)
    add_monitor(sub)
    logs.add(sub)
    args = parser.parse_args(argv)
    args.pytest_args = extra
    return args


def add_run_options(add: Callable[..., argparse.Action]) -> None:
    """What `run` takes besides `--spec`, through `add`, an `add_argument`.
    `vivarium run` and `vivarium up` take the same, so `--help` lists them
    and a missing `--out` fails before an evaluation."""
    add(
        "--out",
        type=_absolute,
        required=True,
        help="where the run's evidence goes: artifacts, log, report, status",
    )
    add(
        "--only",
        action="append",
        default=[],
        metavar="PHASE",
        help="run only this phase and the phases it runs after; repeatable",
    )
    add(
        "--break",
        dest="breaks",
        action="append",
        default=[],
        metavar="POINT",
        help=(
            "pause before this phase, or at `start` (before the first phase),"
            " `failure` (when a phase fails) or `end` (after the last phase),"
            " with the guests up; repeatable. `vivarium ctl` reaches in"
        ),
    )
    add(
        "--kernel",
        type=_kernel_value,
        action="append",
        default=[],
        metavar="[GUEST=]PATH",
        help=(
            "boot this kernel in GUEST, or in every guest not named, instead of"
            " the one Nix built: `linux` from a UML tree, a bzImage for QEMU,"
            " `vmlinux` for krun (virtio built in). A container guest refuses"
            " it. For iterating on a kernel without a Nix build per change;"
            " never the check's kernel; repeatable"
        ),
    )
    add(
        "--cpus",
        type=_cpus_value(int),
        action="append",
        default=[],
        metavar="[GUEST=]N",
        help="N processors for GUEST, or for every guest not named; in place of `vivarium.cpus`; repeatable",
    )
    add(
        "--cpu-scale",
        type=_cpus_value(float),
        action="append",
        default=[],
        metavar="[GUEST=]S",
        help="the host's cores times S (0.5 is half) for GUEST, or for every guest not named; repeatable",
    )
    add(
        "--break-on-start",
        action="store_true",
        help="pause before the first phase, with the guests up",
    )
    add(
        "--interactive",
        action="store_true",
        help=(
            "a Python REPL on this terminal, as nixos-test's driverInteractive:"
            " pause before the first phase, after a failure and after the last"
            " phase; ^D tears the run down"
        ),
    )
    add(
        "--break-on-failure",
        action="store_true",
        help="pause when a phase fails, with the guests up and the state intact",
    )
    add(
        "--hold",
        action="store_true",
        help=(
            "run as an environment: once every phase passed, say `ready` and keep the"
            " guests up until `vivarium ctl stop`, which ends the run with its verdict."
            " A failure pauses, as --break-on-failure does"
        ),
    )
    add(
        "--no-control",
        dest="control",
        action="store_false",
        help=(
            "serve no control socket. For a sandboxed check, where nobody"
            " can reach in and a socket left by a killed run fails the output"
        ),
    )
    add(
        "--serial",
        action="store_true",
        help=("one phase at a time, in the order Nix sorted them, even where `nodes` would let phases run at once"),
    )
    add(
        "--offline",
        action="store_true",
        help=("give the guests no way off this host, the way a sandboxed check has none"),
    )
    add(
        "--random-ports",
        action="store_true",
        help=(
            "bind every forward through an egress on any free host port, as a build does;"
            " without it a forward's port that another process holds fails the boot"
        ),
    )
    add(
        "--keep",
        action="store_true",
        help="remove nothing at the end: guest disks, sockets, the run directory",
    )
    add(
        "--verbose",
        "-v",
        action="count",
        default=0,
        help=(
            "-v shows every command sent to a guest, -vv adds the guests'"
            " consoles. Both are written to the output directory either way"
        ),
    )
    add(
        "--quiet",
        "-q",
        action="store_true",
        help="only failures and the verdict",
    )


def add_ctl(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`ctl`: one request to a run. The runner and the `vivarium` CLI both take it."""
    command = sub.add_parser(
        "ctl", help="reach into a run: exec, inject, state and stop any time, the rest while paused"
    )
    command.add_argument("--out", type=_absolute, required=True, help="the run's --out")
    command.add_argument("--json", action="store_true", help="the whole reply as one JSON object on stdout")
    command.add_argument(
        "--wait",
        action="store_true",
        help=(
            "continue: then wait for the next pause or the verdict, as `monitor --quiet` does,"
            f" and exit with its status ({describe()})"
        ),
    )
    command.add_argument(
        "--reload",
        action="append",
        default=[],
        type=_absolute,
        metavar="DIR",
        help=(
            "inject and pytest: import from this working-tree directory first, and"
            " drop the modules it provides, so an edited helper is read again; repeatable"
        ),
    )
    command.add_argument("op", choices=[str(op) for op in Op])
    command.add_argument(
        "arg",
        nargs="?",
        default="",
        help=(
            "exec: Python, or - for stdin. inject: a file with `test(vms)`."
            " pytest: a test file or directory, pytest's arguments after --."
            " run: a phase. reload: a spec file (`vivarium reload` builds one)."
            " segment: a segment's name"
        ),
    )
    command.add_argument(
        "segment_args",
        nargs="*",
        metavar="ACTION [MEMBER]",
        help=(
            "segment: show (the default), cut or restore (every member, a guest's, or `guest/interface`),"
            " capture or end-capture"
        ),
    )
    command.set_defaults(handler=functools.partial(anyio.run, ctl))


def add_monitor(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """`monitor`: a run's events until its verdict. The runner and the `vivarium` CLI both take it."""
    command = sub.add_parser(
        "monitor",
        help="print a run's events, one line each, until its verdict",
        description=(
            f"Follow a run, live or finished, by its --out or its id. Exit status: {describe()}."
            f" {Exit.NO_RUN.value} is also a stream that ended early, or no run started within --wait."
            " The exit status is the verdict: run it bare, with no pipe and no"
            " `; echo` after it, or a pause reads as success."
        ),
    )
    command.add_argument("target", help="the run's --out directory, or its id")
    command.add_argument("--json", action="store_true", help="each event as a JSON object")
    command.add_argument(
        "--quiet",
        action="store_true",
        help="only pauses, failures and the verdict, not each phase that starts or passes",
    )
    command.add_argument(
        "--through-pauses",
        action="store_true",
        help="print a pause and go on to the verdict, instead of exiting 4 at the pause",
    )
    command.add_argument(
        "--wait",
        type=float,
        default=monitor.WAIT,
        metavar="SECONDS",
        help="how long to wait for a run that has not started (default: %(default)g)",
    )
    command.set_defaults(handler=watch)


def announce(session: Session) -> None:
    """Every knob, its value and where the value came from.

    Before anything boots, and always. A misspelled variable is invisible
    otherwise: the run quietly does the whole suite instead of the one
    case that was asked for, and nothing says why.
    """
    for name, knob in sorted(session.spec.knobs.items()):
        session.emit(
            Kind.KNOB,
            f"knob {name}={knob.value!r} ({knob.source})",
            knob=name,
            value=knob.value,
            source=knob.source,
            env=knob.env,
        )


def terminal_level(args: argparse.Namespace) -> Level:
    """What the person watching asked to see.

    The default leaves the guests' consoles out. Every one of them is
    written to `console/<guest>.log` regardless, and the tail of each is
    replayed when a phase fails -- so nothing is lost by the quiet
    default, and the case that matters is louder than it was.
    """
    if args.quiet:
        return Level.ERROR
    if args.verbose >= 2:
        return Level.CONSOLE
    if args.verbose == 1:
        return Level.DETAIL
    return Level.INFO


def sinks_for(args: argparse.Namespace, name: str) -> Broadcast:
    """Everything that wants the events.

    One stream, four readers: the person, the machine, the guests' own
    consoles, and whatever CI reads JUnit with. Adding a fifth -- an MCP
    server -- is another entry here and nothing else.
    """
    return Broadcast(
        [
            Terminal(terminal_level(args)),
            # Unfiltered on purpose. The terminal is a view; this is the
            # record, and a record that only kept what somebody thought
            # was interesting at the time is not one.
            Log(args.out / "log"),
            JsonLines(args.out / "events.jsonl"),
            ConsoleFiles(args.out / "console"),
            Junit(args.out / "junit.xml", name),
        ]
    )


async def run(args: argparse.Namespace) -> int:
    spec = Spec.read(args.spec)
    sink = sinks_for(args, spec.name)
    session = Session(
        spec,
        args.out,
        offline=args.offline,
        random_ports=args.random_ports,
        sink=sink,
        pytest_args=args.pytest_args,
        kernel=kernels_of(args),
        cpus=cpus_of(args),
    )
    session.roots.hold(args.spec)
    session.emit(Kind.RUN_STARTED, f"output in {args.out}")
    if sys.platform == "linux":
        session.emit(Kind.NOTE, namespace.describe(Path("/proc/self/uid_map").read_text()))
    announce(session)

    if args.only:
        session.deselect(args.only)

    # Refuses a `--cpus` or `--kernel` the guests do not take, and a host
    # that lacks what they need, before the task group: an error raised
    # inside it prints as a traceback.
    session.specs()
    await session.preflight()

    if args.hold and not args.control:
        # A sandboxed check would wait for a stop nobody can send.
        raise SessionError("--hold needs the control socket; it cannot go with --no-control")
    if args.hold and args.offline:
        session.emit(
            Kind.NOTE,
            "--offline: the forwards are in the run's own network namespace;"
            " the host cannot reach the addresses in access.json",
            level=Level.ERROR,
        )

    try:
        before, points = breakpoints(args.breaks, session.spec.phases)
        origin = None if ORIGIN not in os.environ else Origin.from_json(os.environ[ORIGIN])
        # Taken out, so that a run a phase starts does not watch it too.
        parent = os.environ.pop(PARENT, None)
        if parent is not None and not parent.isdigit():
            raise ValueError(f"{PARENT} is not a pid: {parent!r}")
        parent_pid = None if parent is None else int(parent)
    except ValueError as error:
        raise SessionError(str(error)) from None

    # What a signal cancels: the sequence, as `ctl stop` does. Not the
    # drive: its task group holds each guest's agent connection, and with
    # that cancelled the teardown cannot ask a guest to power off.
    sequence = anyio.CancelScope()
    try:
        async with anyio.create_task_group() as group:
            group.start_soon(_stop_on_signal, session, sequence)
            if parent_pid is not None:
                group.start_soon(_stop_with_parent, session, sequence, parent_pid)
            await drive(
                session,
                breaks=before,
                break_on_failure=args.break_on_failure or args.interactive or args.hold or FAILURE in points,
                break_on_start=args.break_on_start or args.interactive or START in points,
                break_at_end=args.interactive or END in points,
                hold=args.hold,
                interactive=args.interactive,
                serial=args.serial,
                control=args.control,
                origin=origin,
                spec=args.spec,
                sequence=sequence,
            )
            group.cancel_scope.cancel()
        if session.stopped is not None:
            return session.stopped.status
        for line in session.report.summary().splitlines():
            session.emit(Kind.NOTE, line.removeprefix("[time] "))
        session.emit(Kind.NOTE, summarise(session.state))
        return 0 if session.passed else 1
    finally:
        sink.close()


async def _stop_on_signal(session: Session, scope: anyio.CancelScope) -> None:
    """SIGTERM or SIGHUP ends the run the way ^C does: the sequence is
    cancelled, and its shielded teardown powers each guest off, then
    signals each guest's process group, SIGTERM and then SIGKILL."""
    with anyio.open_signal_receiver(signal.SIGTERM, signal.SIGHUP) as signals:
        async for received in signals:
            session.stopped = Stop.by_signal(int(received), signal.Signals(received).name)
            session.emit(Kind.NOTE, f"{signal.Signals(received).name}: tearing the guests down")
            scope.cancel()
            return


async def _stop_with_parent(session: Session, scope: anyio.CancelScope, pid: int) -> None:
    """The exit of `PARENT`'s pid ends the run as SIGHUP does.

    The watch is opened before `getppid` is compared: a parent that exited
    before the run started watching then counts as exited too.
    """
    try:
        with runroot.exit_watch(pid) as watch:
            if os.getppid() == pid:
                await anyio.wait_readable(watch)
    except ProcessLookupError:
        pass
    session.stopped = Stop(f"pid {pid} exited", 128 + signal.SIGHUP)
    session.emit(Kind.NOTE, f"pid {pid}, which started this run, exited: tearing the guests down")
    scope.cancel()


async def ctl(args: argparse.Namespace) -> Exit:
    """One request to a run; its output, its value, its error."""
    arg = sys.stdin.read() if args.arg == "-" else args.arg
    # The run resolves a path against its own working directory, not ours.
    if args.op in {Op.INJECT, Op.PYTEST, Op.RELOAD}:
        arg = str(Path(arg).absolute())
    if args.op == Op.PYTEST:
        # `vivarium ctl --out o pytest ./tests -- -k x`, as `vivarium run` takes them.
        arg = shlex.join([arg, *args.pytest_args])
    if args.segment_args and args.op != Op.SEGMENT:
        print(f"[vivarium] {args.op} takes one argument", file=sys.stderr)
        return Exit.USAGE
    if args.op == Op.SEGMENT:
        arg = shlex.join([arg, *args.segment_args])
    socket = places.socket(args.out, SOCKET)
    if args.wait and args.op != Op.CONTINUE:
        print("[vivarium] --wait is for continue", file=sys.stderr)
        return Exit.USAGE
    streamed: list[str] = []

    def show(text: str) -> None:
        # As it comes: a long `pytest` or `exec` is not silent until it ends.
        streamed.append(text)
        print(text, end="", flush=True)

    try:
        if args.op == Op.WATCH:
            return await monitor.follow(socket, as_json=args.json, until_pause=False)
        chunk = None if args.json else show
        reply = await request(socket, Op(args.op), arg, tuple(args.reload), chunk)
    except OSError as error:
        print(f"[vivarium] no run is listening at {socket}: {error}", file=sys.stderr)
        return Exit.NO_RUN
    except WatchRefused as error:
        print(f"[vivarium] the run at {socket} serves no watch: {error}", file=sys.stderr)
        return Exit.REFUSED
    if args.op == Op.CONTINUE and reply.ok:
        # Returns once the run has said `resumed`, so a monitor armed now
        # replays no open pause.
        with anyio.move_on_after(10), contextlib.suppress(OSError):
            await _resumed(socket)
        if args.wait:
            try:
                return await monitor.follow(socket, quiet=True, replay=False)
            except (OSError, WatchRefused) as error:
                print(f"[vivarium] cannot wait on {socket}: {error}", file=sys.stderr)
                return Exit.NO_RUN
    if args.op == Op.STOP and reply.ok:
        if await wait_for_exit(args.out):
            reply.result = "stopped"
        else:
            reply.ok = False
            reply.error = f"the run did not exit within {STOP_WAIT:g}s of stop"
    if args.json:
        print(json.dumps(dataclasses.asdict(reply)))
        return reply.status
    if streamed:
        # The reply's output is what was printed already.
        if not streamed[-1].endswith("\n"):
            print()
    elif reply.output:
        print(reply.output, end="" if reply.output.endswith("\n") else "\n")
    if reply.result is not None:
        print(reply.result if reply.pause is None else f"{reply.result} {reply.pause}")
    if reply.state:
        errors = reply.errors or {}
        for name, state in reply.state.items():
            why = errors.get(name, "").strip().splitlines()[:1]
            print("\t".join([name, state, *why]))
    if reply.error:
        print(reply.error.rstrip(), file=sys.stderr)
    return reply.status


async def _resumed(socket: Path) -> None:
    """Until the run's pause is closed: no open pause once `watch` is live."""
    seen: list[dict[str, str]] = []
    live = False
    try:
        async for event in watch_run(socket):
            if event == monitor.LIVE:
                live = True
            else:
                seen.append(event)
            if live and not monitor.paused_now(seen):
                return
    except WatchRefused:
        # A runner with no `watch`: ask until `state` says running.
        while (await request(socket, Op.STATE)).result == "paused":
            await anyio.sleep(0.05)


async def wait_for_exit(out: Path) -> bool:
    """Whether the run in `out` exited within `STOP_WAIT`: its pid from
    `run.json`, through `runroot.exit_watch`, so the exit wakes this and
    nothing polls."""
    about = RunFile.from_json(await anyio.Path(out / RUN_FILE).read_text())
    try:
        with runroot.exit_watch(about.pid) as watch:
            with anyio.move_on_after(STOP_WAIT):
                await anyio.wait_readable(watch)
                return True
            return False
    except ProcessLookupError:
        return True


async def phases(args: argparse.Namespace) -> int:
    spec = Spec.read(args.spec)
    for phase in spec.phases:
        after = f" after {', '.join(phase.after)}" if phase.after else ""
        print(f"{phase.name}{after}")
    return 0


def watch(args: argparse.Namespace) -> int:
    """`monitor`: follow a run until its verdict; the exit status."""
    out = monitor.directory_of(args.target)
    socket = places.socket(out, SOCKET)
    options = {"as_json": args.json, "quiet": args.quiet, "until_pause": not args.through_pauses}
    try:
        arrival = anyio.run(monitor.wait_for_run, out, args.wait, monitor.Watch(**options))
        if arrival is monitor.Arrival.GONE:
            return Exit.NO_VERDICT
        if arrival is monitor.Arrival.NEVER:
            print(
                f"[vivarium] no run in {out} after {args.wait:g}s: no events.jsonl",
                file=sys.stderr,
                flush=True,
            )
            return Exit.NO_RUN
        try:
            return anyio.run(functools.partial(monitor.follow, socket, **options))
        except (FileNotFoundError, ConnectionRefusedError, WatchRefused):
            # No `watch` here: `--no-control`, an older runner, or a run
            # gone without removing its socket. Follow what it writes.
            pass
        if not (out / "events.jsonl").exists():
            print(f"[vivarium] no run in {out}: no events.jsonl", file=sys.stderr, flush=True)
            return Exit.NO_RUN
        return anyio.run(functools.partial(monitor.follow_file, out, **options))
    except KeyboardInterrupt:
        return 130


def run_command(args: argparse.Namespace) -> int:
    if args.keep:
        os.environ[runroot.KEEP] = "1"
    registry.pin()
    # First, before anything boots or any thread starts.
    namespace.enter(Spec.read(args.spec).unshare, private_net=args.offline)
    args.out.mkdir(parents=True, exist_ok=True)
    # Held to the end of the process; the kernel lets go of it then.
    liveness.hold(args.out)
    try:
        return anyio.run(run, args)
    finally:
        runroot.remove_own()


def main(argv: list[str] | None = None) -> None:
    args = parse(argv)
    try:
        raise SystemExit(args.handler(args))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except (SessionError, SpecError, namespace.NamespaceError, liveness.RunLive) as error:
        print(f"[vivarium] {error}", file=sys.stderr, flush=True)
        raise SystemExit(Exit.NO_VERDICT) from None


if __name__ == "__main__":
    main()
