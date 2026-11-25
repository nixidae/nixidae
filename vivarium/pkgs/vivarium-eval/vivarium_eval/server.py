"""`vivarium-mcp`: runs as tools, and what happens to them as channel events.

An MCP server over stdio. `start` launches a run as a child process with
`--break-on-failure` on; the other tools are the control socket's
operations and a query over `events.jsonl`. When the run pauses, fails a
phase or finishes, a `notifications/claude/channel` event is pushed, so
Claude Code hears about it without polling:

    <channel source="vivarium" run="vivarium-pytest-x1" event="paused" reason="after cases failed">...

**The run is a child process, never this one.** MCP's stdio transport
is this process's stdout, and one stray line there corrupts the
protocol: a print from a phase, a library or the runner itself. So a
run is `vivarium run ...` (or `python -m vivarium.cli run` for a spec)
with its output in `<out>/terminal.log`, and this process
is one more client of `<out>/control.sock`, as `vivarium ctl` is.

Channels are a Claude Code research preview. The server declares the
`claude/channel` capability, and until it is on the allowlist Claude
Code takes its events only with
`claude --dangerously-load-development-channels server:vivarium`. Without it
the tools work and the events are dropped, so `state` and `events`
still answer.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, Protocol, TypedDict

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification
from vivarium import logs, places, registry
from vivarium.control import PARENT, RUN_FILE, SOCKET, Op, Reply, RunFile, WatchRefused, request, watch
from vivarium.events import Event, Kind, NotAnEvent
from vivarium.exits import describe
from vivarium.journal import Tail
from vivarium.monitor import LIVE, Channel, channel_event, clip, paused_now
from vivarium.phases import VERDICT, Verdict
from vivarium.registry import ASK
from vivarium.runroot import exit_watch

from .cli import split_attr

if TYPE_CHECKING:
    from anyio.abc import TaskGroup
    from anyio.streams.memory import MemoryObjectSendStream

CHANNEL: Final = "notifications/claude/channel"

STOP_WAIT: Final = 60.0
"""Seconds a stopped run has for its teardown before this server gives up on it."""

INSTRUCTIONS: Final = f"""\
Runs NixOS guests under vivarium and lets you reach into them.

`start` launches a run in the background and returns its id at once; it
pauses on the first failing phase with the guests still up. Every tool
takes a run's id or its `--out`: a run another program started
(`vivarium up`, the pytest plugin, a shell) is attached at first use, and
`runs` lists every run up on this host. `exec` runs
Python against the live guests (top-level await; `vms`, `session` and
each guest by name are in scope, and names persist between calls), and
`inject` runs a local file's `async def test(vms)`; both work while
phases run, not only while paused. `switch` adds a module file to one
guest and switches it, evaluated and built on the host; `reload`
evaluates the test again and gives a paused run its edited phases, guests
kept. `screenshot`
shows a guest's screen (`vivarium.display.enable`); act on it through
`exec` with `click`, `send_chars`, `send_key`, `click_text` and
`wait_for_text`. While paused, `run_pytest` runs local
pytest tests, `run_phase` runs a declared phase, and `resume`
continues. `inject` and `run_pytest` read the file each time, so the loop
for a failing test is: edit it, send it again, against the same guests.
`events` queries the run's event stream: filter by kind (journal, case,
phase_finished, rpc, output, error), machine, unit, phase or case.

Events arrive as <channel source="vivarium" run="..." event="progress|paused|ready|resumed|failed|finished|exited" ...>.
`ready` is a run started with `hold`: its phases passed and its guests
stay up until `stop`.
A `progress` or `resumed` event marks the run moving on; say one line
about it so the person watching sees the run move, and do nothing else.
Without channels, run the `monitor` command that `start` returns in a
Monitor or in the background: it prints pauses, failures and the verdict,
one line each, and exits with its status ({describe()}).
Run it again after each `resume`.
`monitor_all` prints every event, progress included, through pauses to
the verdict.
On `paused`, look with `events` and `exec` before you `resume` -- the
guests go down when the run ends. `stop` ends a run early and still
tears the guests down.
"""


# ── pure ────────────────────────────────────────────────────────────


class EventLine(TypedDict, total=False):
    """One line of `events.jsonl`. `data` has a different shape for each kind."""

    kind: str
    machine: str
    phase: str
    text: str
    data: dict[str, Any]


def select(
    lines: list[str],
    *,
    kind: str | None = None,
    machine: str | None = None,
    unit: str | None = None,
    phase: str | None = None,
    case: str | None = None,
    contains: str | None = None,
    limit: int = 50,
) -> list[EventLine]:
    """The last `limit` events that match every filter given."""
    wanted = logs.Filter(
        kinds=() if kind is None else (kind,),
        machines=() if machine is None else (machine,),
        units=() if unit is None else (unit,),
        phase=phase,
        case=case,
        contains=contains,
    )
    found: list[EventLine] = [json.loads(line) for line, event in logs.parsed(lines) if wanted.matches(event)]
    return found[-limit:] if limit > 0 else found


BUDGET: Final = 40_000
"""Characters of events one `events` reply carries. Measured: one
unfiltered `case` query was 233k characters."""


def budgeted(events: list[EventLine], budget: int = BUDGET) -> tuple[list[EventLine], int]:
    """The newest of `events` that fit in `budget` characters, each text
    clipped, and how many older ones did not fit. The newest one is kept
    whatever its size."""
    kept: list[EventLine] = []
    used = 0
    for event in reversed(events):
        short: EventLine = clipped(event, "events.jsonl")
        size = len(json.dumps(short))
        if kept and used + size > budget:
            break
        kept.append(short)
        used += size
    return kept[::-1], len(events) - len(kept)


def _per_guest(value: float | dict[str, int] | dict[str, float] | None) -> dict[str | None, float]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return {guest: number for guest, number in value.items()}
    return {None: value}


def cpu_flags(cpus: int | dict[str, int] | None, cpu_scale: float | dict[str, float] | None) -> list[str]:
    """`--cpus` and `--cpu-scale` for the run: a number is every guest not
    named, a mapping one count or scale per guest. Each guest takes one."""
    counts, scales = _per_guest(cpus), _per_guest(cpu_scale)
    both = set(counts) & set(scales)
    if both:
        names = ", ".join("every guest" if guest is None else guest for guest in sorted(both, key=str))
        raise ValueError(f"give {names} either cpus or cpu_scale, not both")
    flags: list[str] = []
    for flag, given in (("--cpus", counts), ("--cpu-scale", scales)):
        for guest, number in given.items():
            flags += [flag, str(number) if guest is None else f"{guest}={number}"]
    return flags


def kernel_flags(kernel: str | dict[str, str] | None) -> list[str]:
    """`--kernel` for the run: a path is every guest not named, a mapping one
    kernel per guest. Each path is made absolute here, in the caller's
    directory."""
    flags: list[str] = []
    for guest, path in ({None: kernel} if isinstance(kernel, str) else kernel or {}).items():
        absolute = str(Path(path).absolute())
        flags += ["--kernel", absolute if guest is None else f"{guest}={absolute}"]
    return flags


def run_argv(
    *,
    out: Path,
    attr: str | None,
    spec: str | None,
    file: str,
    breaks: list[str],
    break_on_failure: bool,
    only: list[str],
    offline: bool,
    pytest_args: list[str],
    random_ports: bool = False,
    runner: Path | None = None,
    kernel: str | dict[str, str] | None = None,
    cpus: int | dict[str, int] | None = None,
    cpu_scale: float | dict[str, float] | None = None,
    hold: bool = False,
) -> list[str]:
    """The child's command line. By attribute through the `vivarium` CLI, which
    evaluates first; by spec straight to the `vivarium` the spec names."""
    if (attr is None) == (spec is None):
        raise ValueError("give exactly one of attr and spec")
    bin_dir = Path(sys.executable).parent
    if attr is not None:
        split_attr(attr)
        head = [str(bin_dir / "vivarium"), "run", attr, "--file", file]
    elif runner is not None:
        # The spec's own `vivarium`, which knows every field in it.
        head = [str(runner / "bin" / "vivarium-runner"), "run", "--spec", str(spec)]
    else:
        head = [sys.executable, "-m", "vivarium.cli", "run", "--spec", str(spec)]
    argv = [*head, "--out", str(out)]
    for name in breaks:
        argv += ["--break", name]
    for name in only:
        argv += ["--only", name]
    if break_on_failure:
        argv.append("--break-on-failure")
    if offline:
        argv.append("--offline")
    if random_ports:
        argv.append("--random-ports")
    if hold:
        argv.append("--hold")
    argv += kernel_flags(kernel)
    argv += cpu_flags(cpus, cpu_scale)
    if pytest_args:
        argv += ["--", *pytest_args]
    return argv


# ── runs ────────────────────────────────────────────────────────────


class Child(Protocol):
    """What a run needs of its process. `anyio`'s `Process` has all of it."""

    @property
    def returncode(self) -> int | None: ...

    def send_signal(self, sig: signal.Signals) -> None: ...

    async def wait(self) -> int: ...

    def kill(self) -> None: ...


@dataclass(frozen=True)
class Source:
    """Where a run by attribute came from, to evaluate it again."""

    attr: str
    file: str
    env: dict[str, str]


@dataclass(kw_only=True)
class Run(ABC):
    """A run a tool reaches: one this server started, or one it attached to."""

    id: str
    out: Path
    source: Source | None = None
    finished: bool = False
    backlog: list[dict[str, str]] = field(default_factory=list)
    """Every event pushed for this run: `resume` reads whether a pause is open."""
    pushed: anyio.Event = field(default_factory=anyio.Event)
    """Set, and replaced, at each push: what `resume` waits on."""
    replies: int = 0
    """Replies so far: names the file a clipped reply is written to."""

    HISTORY: ClassVar[bool] = True
    """Whether the events `watch` replays go to the channel. A run this
    server started has none it did not see; an attached run's are old."""

    def took(self, event: dict[str, str]) -> None:
        self.backlog.append(event)
        self.pushed.set()
        self.pushed = anyio.Event()

    @property
    def socket(self) -> Path:
        return places.socket(self.out, SOCKET)

    @property
    @abstractmethod
    def running(self) -> bool: ...

    @property
    @abstractmethod
    def status(self) -> str: ...

    @abstractmethod
    async def ended(self) -> None:
        """Until the run's process has exited."""

    @abstractmethod
    async def stop(self) -> None: ...


@dataclass(kw_only=True)
class Started(Run):
    """A run this server started. It goes down with the server."""

    process: Child

    @property
    def running(self) -> bool:
        return self.process.returncode is None

    @property
    def status(self) -> str:
        return "running" if self.running else f"exited {self.process.returncode}"

    async def ended(self) -> None:
        await self.process.wait()

    async def stop(self) -> None:
        """`ctl stop` through the socket, so the verdict says stopped; SIGINT
        when the run has no socket or does not know `stop`. The run answers
        either with its shielded teardown."""
        if not self.running:
            return
        asked = False
        # Bounded: a run that hangs on its socket still gets the signal.
        with anyio.move_on_after(10), contextlib.suppress(OSError):
            asked = (await request(self.socket, Op.STOP)).ok
        if not asked:
            self.process.send_signal(signal.SIGINT)
        with anyio.move_on_after(STOP_WAIT):
            await self.process.wait()
            return
        self.process.kill()


@dataclass(kw_only=True)
class Attached(Run):
    """A run another program started: `vivarium up`, the pytest plugin, a
    shell. It outlives this server, so the server never stops it on exit."""

    HISTORY: ClassVar[bool] = False

    gone: anyio.Event = field(default_factory=anyio.Event)

    @property
    def running(self) -> bool:
        return not self.gone.is_set()

    @property
    def status(self) -> str:
        return "running" if self.running else "ended"

    async def ended(self) -> None:
        await self.gone.wait()

    async def stop(self) -> None:
        """`ctl stop`; the run has no other way in from here."""
        if not self.running:
            return
        with contextlib.suppress(OSError):
            await request(self.socket, Op.STOP)
        with anyio.move_on_after(STOP_WAIT):
            await self.ended()


class Runs:
    """Every run a tool reached, and the push channel for them."""

    def __init__(self, group: TaskGroup, push: MemoryObjectSendStream[SessionMessage]) -> None:
        self.group = group
        self.push_stream = push
        self.runs: dict[str, Run] = {}

    async def push(self, content: str, meta: dict[str, str]) -> None:
        """To the channel, and to the run's backlog."""
        run = self.runs.get(meta["run"])
        if run is not None:
            run.took({**meta, "text": content})
        notification = JSONRPCNotification(jsonrpc="2.0", method=CHANNEL, params={"content": content, "meta": meta})
        await self.push_stream.send(SessionMessage(message=JSONRPCMessage(notification)))

    async def start(self, argv: list[str], out: Path, env: dict[str, str], source: Source | None = None) -> Started:
        log = (out / "terminal.log").open("wb")
        process = await anyio.open_process(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            # The run goes down with this server: nobody else attaches to a pause it holds.
            env={**os.environ, **env, PARENT: str(os.getpid())},
        )
        log.close()
        run = Started(id=out.name, out=out, process=process, source=source)
        self.runs[run.id] = run
        self.group.start_soon(self._watch, run)
        return run

    async def attach(self, out: Path) -> Attached:
        """A run another program started, by its `--out`. Raises ValueError
        when `out` holds no run."""
        # Resolved, as the run registers itself: the registry and this agree.
        out = out.resolve()
        try:
            about = RunFile.from_json((out / RUN_FILE).read_text())
        except FileNotFoundError:
            raise ValueError(f"no run in {out}: it has no {RUN_FILE}") from None
        except (OSError, ValueError, KeyError) as error:
            raise ValueError(f"no run in {out}: {RUN_FILE} is unreadable: {error!r}") from error
        origin = about.origin
        source = None if origin is None else Source(attr=origin.attr, file=str(origin.file), env={})
        run = Attached(id=str(out), out=out, source=source)
        self.runs[run.id] = run
        if await _answers(run.socket):
            self.group.start_soon(_until_exit, run, about.pid)
            self.group.start_soon(self._watch, run)
        else:
            # The socket says a run is up, as for `vivarium ls`: a pid in
            # `run.json` may be another process's by now.
            run.gone.set()
        return run

    async def get(self, run: str) -> Run:
        """A run by its id, or by the `--out` of any run on this host."""
        if run in self.runs:
            return self.runs[run]
        out = Path(run).resolve()
        for found in self.runs.values():
            if found.out.resolve() == out:
                return found
        if Path(run).is_dir():
            return await self.attach(Path(run))
        have = ", ".join(self.runs) or "none"
        raise ValueError(f"no run {run!r}: give an id ({have}) or a run's --out; `runs` lists the runs on this host")

    async def _watch(self, run: Run) -> None:
        """Push what matters of the run's events, until it exits: from
        `watch` on its control socket, or from `events.jsonl` for a runner
        that serves none."""
        live = run.HISTORY
        try:
            if await _bound(run):
                async for event in watch(run.socket):
                    if event == LIVE:
                        live = True
                        continue
                    run.finished = run.finished or event.get("event") == Channel.FINISHED
                    # The run names itself by its directory; a tool names it by this id.
                    meta = {key: value for key, value in event.items() if key != "text"} | {"run": run.id}
                    if live:
                        await self.push(event.get("text", ""), meta)
                    else:
                        run.took({**meta, "text": event.get("text", "")})
        except WatchRefused:
            await self._tail(run)
        except OSError:
            # The run went away between binding its socket and our connect.
            pass
        await run.ended()
        if not run.finished:
            # Died before a verdict: an evaluation error, a crash. The
            # reason is at the end of what it printed.
            tail = clip(why_it_exited(_read(run.out / "terminal.log")), str(run.out / "terminal.log"))
            text = f"run {run.status} without a verdict"
            await self.push(f"{text}:\n{tail}" if tail else text, {"run": run.id, "event": Channel.EXITED})

    async def _tail(self, run: Run) -> None:
        """`_watch` for an older runner: polls `events.jsonl`, the only
        thing such a run offers."""
        tail = Tail(run.out / "events.jsonl")
        while True:
            for line in await tail.read():
                try:
                    event = Event.from_dict(json.loads(line))
                except (json.JSONDecodeError, NotAnEvent):
                    continue
                run.finished = run.finished or event.kind is Kind.RUN_FINISHED
                pushed = channel_event(event, run.id)
                if pushed is not None:
                    await self.push(*pushed)
            if not run.running:
                return
            await anyio.sleep(0.25)

    async def stop_all(self) -> None:
        """Every run this server started, at once: one after another, each
        run's bound adds up. An attached run is someone else's."""
        async with anyio.create_task_group() as group:
            for run in self.runs.values():
                if isinstance(run, Started):
                    group.start_soon(run.stop)


async def _answers(socket: Path) -> bool:
    """Whether a run serves `socket`: the test `vivarium ls` applies."""
    try:
        with anyio.fail_after(ASK):
            await request(socket, Op.STATE)
    except (OSError, TimeoutError):
        return False
    return True


async def _until_exit(run: Attached, pid: int) -> None:
    try:
        with exit_watch(pid) as descriptor:
            await anyio.wait_readable(descriptor)
    except ProcessLookupError:
        pass
    run.gone.set()


async def _bound(run: Run) -> bool:
    """Whether the run binds its control socket before it exits.

    Polls: the child evaluates and builds first, then binds the socket,
    and says so nowhere a process can wait on."""
    socket = anyio.Path(run.socket)
    while run.running:
        if await socket.exists():
            return True
        await anyio.sleep(0.25)
    return False


def _directories(paths: list[str] | None) -> tuple[Path, ...]:
    return tuple(Path(path).absolute() for path in paths or [])


def _spec(spec: Path) -> dict[str, Any]:
    """The fields of a spec this server reads: its name, since the file
    name is a store hash, and the `vivarium` that knows the rest."""
    try:
        found: dict[str, Any] = json.loads(spec.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return found


def _read(path: Path) -> str:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return ""


def why_it_exited(output: str, lines: int = 15) -> str:
    """The part of a dead run's output that says why.

    An evaluation failure is printed point first by the `vivarium` CLI, with the
    Nix trace after it, so its opening lines are the ones to show; the
    last lines would be the trace. Anything else died at the end.
    """
    text = output.splitlines()
    for index, line in enumerate(text):
        if line.startswith("[vivarium] evaluation failed:"):
            return "\n".join(text[index : index + 4])
    return "\n".join(text[-lines:])


def clipped(value: Any, where: str) -> Any:  # noqa: ANN401 -- a JSON value of any shape
    """`value` with each string in it clipped, a mapping's values too."""
    if isinstance(value, str):
        return clip(value, where)
    if isinstance(value, dict):
        return {key: clipped(inner, where) for key, inner in value.items()}
    return value


def _reply(reply: Reply, run: Run) -> dict[str, Any]:
    return bounded({key: value for key, value in vars(reply).items() if value not in (None, "")}, run)


async def answer(run: Run, op: Op, arg: str = "", reload: tuple[Path, ...] = ()) -> dict[str, Any]:
    """`ask`, as a tool replies."""
    return _reply(await ask(run, op, arg, reload), run)


def bounded(reply: dict[str, Any], run: Run) -> dict[str, Any]:
    """`reply` with each long text clipped. A clipped reply is written whole
    to `<out>/mcp/`, and the clipped text names that file."""
    run.replies += 1
    # The pid as well: a second server may reach the same run.
    where = run.out / "mcp" / f"{os.getpid()}-{run.replies}.json"
    short: dict[str, Any] = clipped(reply, str(where))
    if short != reply:
        where.parent.mkdir(exist_ok=True)
        where.write_text(json.dumps(reply, indent=2) + "\n")
    return short


def unreachable(run: Run) -> str:
    """Why `run` has no control socket to answer a tool."""
    if not run.running:
        return f"the run has {run.status}; `state` gives its verdict"
    try:
        about = RunFile.from_json((run.out / RUN_FILE).read_text())
    except (OSError, ValueError, KeyError):
        about = None
    if about is not None and about.socket is None:
        return "the run has no control socket (--no-control); `events` and `state` still answer"
    return f"the run is still evaluating or building; it binds {run.socket} before its first guest boots"


async def ask(run: Run, op: Op, arg: str = "", reload: tuple[Path, ...] = ()) -> Reply:
    """One request to the run, or a reply that says why the run cannot take it."""
    try:
        return await request(run.socket, op, arg, reload)
    except (FileNotFoundError, ConnectionRefusedError):
        return Reply(ok=False, error=unreachable(run))


def verdict(run: Run) -> dict[str, Any]:
    """`state` of a run that has ended, from its `phases.json`."""
    try:
        found = Verdict.from_json((run.out / VERDICT).read_text())
    except (OSError, ValueError) as error:
        return {"run": run.id, "status": run.status, "error": f"no verdict in {run.out / VERDICT}: {error}"}
    reply: dict[str, Any] = {
        "run": run.id,
        "status": run.status,
        "passed": found.passed,
        "state": {phase.name: str(phase.state) for phase in found.phases},
    }
    if found.stopped is not None:
        reply["stopped"] = found.stopped
    errors = {phase.name: clip(phase.error, str(run.out / VERDICT)) for phase in found.phases if phase.error}
    if errors:
        reply["errors"] = errors
    return reply


async def evaluate_again(run: Run, verb: str, args: list[str]) -> dict[str, Any]:
    """`vivarium switch` or `vivarium reload` on the run's own attribute."""
    if run.source is None:
        return {"ok": False, "error": f"this run was not started by attribute; {verb} needs one that was"}
    argv = [
        str(Path(sys.executable).parent / "vivarium"),
        verb,
        run.source.attr,
        "--file",
        run.source.file,
        "--out",
        str(run.out),
        *args,
    ]
    done = await anyio.run_process(
        argv,
        check=False,
        stdin=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        env={**os.environ, **run.source.env},
    )
    return bounded({"ok": done.returncode == 0, "output": done.stdout.decode(errors="replace")}, run)


# ── the server ──────────────────────────────────────────────────────


def build(runs_holder: list[Runs]) -> FastMCP:
    """The tools, over whichever `Runs` the running server put in the
    holder -- it needs the task group and the write stream, which exist
    only once `serve` is running."""
    server = FastMCP("vivarium", instructions=INSTRUCTIONS)

    def runs() -> Runs:
        return runs_holder[0]

    @server.tool()
    async def start(
        attr: str | None = None,
        spec: str | None = None,
        file: str = ".",
        breaks: list[str] | None = None,
        break_on_failure: bool = True,
        only: list[str] | None = None,
        offline: bool = False,
        random_ports: bool = True,
        pytest_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        kernel: str | dict[str, str] | None = None,
        cpus: int | dict[str, int] | None = None,
        cpu_scale: float | dict[str, float] | None = None,
        hold: bool = False,
    ) -> dict[str, str]:
        """Start a run in the background; returns its id at once.

        `attr` is evaluated from `file` (a directory means its default.nix),
        or give `spec`, a spec path. `breaks` pauses before those phases,
        or at `start`, `failure` or `end`;
        `break_on_failure` pauses on a failed phase. `env` is added to the
        run's environment, which the evaluation reads: a knob's variable.
        `kernel` boots a kernel from a working tree instead of Nix's:
        `linux` from a UML build, or a bzImage with virtio built in; a path
        is every guest, `{"db": path}` one guest.
        `cpus` is a processor count, `cpu_scale` a share of the host's
        cores (0.5 is half); a number is every guest, `{"db": 8}` one
        guest. Either replaces `vivarium.cpus` for this run.
        `random_ports` binds each forward on any free host port, so runs
        beside other agents' never collide; `access.json` names the ports.
        `hold` runs it as an environment: once every phase passed it is
        `ready`, its guests stay up, and `stop` ends it with its verdict;
        `<out>/access.json` and `<out>/ssh_config` are the way in.
        Events arrive on the vivarium channel. `monitor` in the reply is a
        command that prints the pauses, failures and verdict, one line
        each, and exits 4 at a pause; `monitor_all` prints every event
        through pauses to the verdict. `state` and `events` answer
        meanwhile."""
        written = _spec(Path(str(spec))) if spec is not None else {}
        name = (attr or str(written.get("name", "run"))).replace(".", "-")
        out = Path(tempfile.mkdtemp(prefix=f"vivarium-{name}-"))
        argv = run_argv(
            out=out,
            attr=attr,
            spec=spec,
            file=str(Path(file).absolute()),
            breaks=breaks or [],
            break_on_failure=break_on_failure,
            only=only or [],
            offline=offline,
            pytest_args=pytest_args or [],
            random_ports=random_ports,
            runner=Path(written["vivarium"]) if written.get("vivarium") else None,
            kernel=kernel,
            cpus=cpus,
            cpu_scale=cpu_scale,
            hold=hold,
        )
        source = Source(attr=attr, file=str(Path(file).absolute()), env=env or {}) if attr is not None else None
        run = await runs().start(argv, out, env or {}, source)
        every = [sys.executable, "-m", "vivarium.cli", "monitor", str(out)]
        return {
            "run": run.id,
            "out": str(out),
            "monitor": shlex.join([*every, "--quiet"]),
            "monitor_all": shlex.join([*every, "--through-pauses"]),
        }

    @server.tool()
    async def attach(out: str) -> dict[str, Any]:
        """Reach a run this server did not start, by its `--out`: a `vivarium
        up` environment, a pytest plugin run, a run from a shell. Every tool
        also takes an `--out` as `run` and attaches by itself. The run's
        events from now on arrive on the channel; this server never stops it
        when it exits."""
        found = await runs().get(out)
        return {"run": found.id, "out": str(found.out), "status": found.status}

    @server.tool()
    async def state(run: str) -> dict[str, Any]:
        """Each phase's state, and whether the run is paused, running or gone."""
        found = await runs().get(run)
        if not found.running:
            return verdict(found)
        return await answer(found, Op.STATE)

    @server.tool(name="exec")
    async def exec_(run: str, code: str) -> dict[str, Any]:
        """Run Python in the run, paused or running. Top-level await;
        `vms`, `session` and each guest by name are in scope; names
        persist between calls. The last expression's repr is `result`."""
        return await answer(await runs().get(run), Op.EXEC, code)

    @server.tool()
    async def inject(run: str, path: str, reload: list[str] | None = None) -> dict[str, Any]:
        """Run a local file's `async def test(vms)` in the run, paused or
        running, read fresh from disk, so an edit takes effect by
        injecting it again. `reload` names working-tree directories of
        helper modules: imported from first, their modules dropped, so an
        edited helper is read again too."""
        found = await runs().get(run)
        return await answer(found, Op.INJECT, str(Path(path).absolute()), _directories(reload))

    @server.tool()
    async def run_pytest(
        run: str, path: str, args: list[str] | None = None, reload: list[str] | None = None
    ) -> dict[str, Any]:
        """Run pytest on a local test file or directory against the paused
        guests, read fresh from disk: edit a test and run it again without
        rebuilding the setup. `args` go to pytest (`["-k", "etcd"]`). The
        cases are events marked by_hand and do not count toward the run's
        verdict. `reload` is as for `inject`."""
        arg = shlex.join([str(Path(path).absolute()), *(args or [])])
        return await answer(await runs().get(run), Op.PYTEST, arg, _directories(reload))

    @server.tool()
    async def run_phase(run: str, phase: str) -> dict[str, Any]:
        """Run a declared phase now, in the paused run."""
        return await answer(await runs().get(run), Op.RUN, phase)

    @server.tool()
    async def segment(run: str, name: str, action: str = "show", member: str | None = None) -> dict[str, Any]:
        """Cut, restore or capture an Ethernet segment, paused or running.

        `action` is `show`, `cut`, `restore`, `capture` or `end-capture`.
        `cut` and `restore` take a `member`: a guest (`a`), or one
        interface (`a/eth1`); none is every member. A cut keeps the
        carrier up. `capture` writes `<out>/pcap/<name>.pcap`. Only a hub
        segment (`segments.<name>.hub`, or three members or more) can do
        either."""
        arg = shlex.join([name, action, *([member] if member is not None else [])])
        return await answer(await runs().get(run), Op.SEGMENT, arg)

    @server.tool()
    async def resume(run: str) -> dict[str, Any]:
        """Continue a paused run."""
        found = await runs().get(run)
        reply = await ask(found, Op.CONTINUE)
        # Until the pause is resolved in the backlog: a `monitor`
        # armed on this reply would otherwise replay the pause and exit.
        with anyio.move_on_after(10):
            while reply.ok and paused_now(found.backlog) and found.running:
                await found.pushed.wait()
        return _reply(reply, found)

    @server.tool()
    async def switch(run: str, node: str, module: str, action: str = "switch") -> dict[str, Any]:
        """Add a NixOS module file to one guest of a running session and
        switch it: the host evaluates the session again with the module on
        `node`, builds it, and the guest switches to the result. `action` is
        switch-to-configuration's: switch, test, boot or dry-activate. Only
        for a run started by attribute; edit the file and call again to iterate."""
        found = await runs().get(run)
        return await evaluate_again(
            found, "switch", ["--node", node, "--module", str(Path(module).absolute()), "--action", action]
        )

    @server.tool()
    async def reload(run: str) -> dict[str, Any]:
        """Evaluate a paused session again and give the run its edited
        phases: scripts, pytest specs, `after`, `nodes`, settings. The guests
        stay; a change to a guest is refused (use `switch`). A phase that ran
        keeps its verdict until `run_phase` runs it again. Only for a run
        started by attribute."""
        return await evaluate_again(await runs().get(run), "reload", [])

    # Unstructured: FastMCP would otherwise try to serialise the image
    # as JSON, and fails on it.
    @server.tool(structured_output=False)
    async def screenshot(run: str, machine: str, name: str | None = None) -> list[Any]:
        """A guest's screen as an image, saved in its artifacts too. The
        guest needs `vivarium.display.enable`. To act on what you see, use
        `exec`: `await m.click(x, y)`, `await m.send_chars("text\\n")`,
        `await m.send_key("ctrl-alt-f2")`, `await m.click_text("OK")`,
        `await m.wait_for_text("regex")`; pixels count from the top left."""
        code = f"print(await vms[{machine!r}].screenshot({name!r}))"
        found = await runs().get(run)
        reply = await ask(found, Op.EXEC, code)
        lines = (reply.output or "").strip().splitlines()
        if not reply.ok or not lines:
            return [json.dumps(_reply(reply, found))]
        path = Path(lines[-1])
        return [Image(data=path.read_bytes(), format="png"), str(path)]

    @server.tool()
    async def stop(run: str) -> dict[str, Any]:
        """End a run now. The evidence is written and the guests go down.
        The reply is the run's verdict."""
        found = await runs().get(run)
        await found.stop()
        return verdict(found)

    @server.tool()
    async def events(
        run: str,
        kind: str | None = None,
        machine: str | None = None,
        unit: str | None = None,
        phase: str | None = None,
        case: str | None = None,
        contains: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        """The run's events, newest last, filtered. `kind="journal", machine="cp",
        unit="kubelet.service"` is one service on one guest; `kind="case"` is
        each pytest test; `kind="error"` is every traceback. `limit=0` is
        every match. A reply holds about 40k characters at most: `dropped`
        counts the older matches left out, so filter more."""
        path = (await runs().get(run)).out / "events.jsonl"
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return {"events": []}
        found = select(
            lines, kind=kind, machine=machine, unit=unit, phase=phase, case=case, contains=contains, limit=limit
        )
        kept, dropped = budgeted(found)
        return {"events": kept, "dropped": dropped} if dropped else {"events": kept}

    # Every tool returns an object. FastMCP wraps any other return type
    # as `{"result": ...}`, and `exec`'s own reply has a `result` key, so
    # a client could not tell the two apart.
    @server.tool(name="runs")
    async def list_runs() -> dict[str, list[dict[str, str]]]:
        """Every run a tool reached, then every other run up on this host
        (as `vivarium ls`). Give a run's `run` to any tool."""
        known = runs().runs.values()
        listed = [{"run": found.id, "out": str(found.out), "status": found.status} for found in known]
        reached = {found.out for found in known}
        for up in await registry.runs():
            if up.out not in reached:
                listed.append({"run": str(up.out), "out": str(up.out), "status": up.state, "name": up.name or ""})
        return {"runs": listed}

    return server


async def serve() -> None:
    holder: list[Runs] = []
    server = build(holder)
    lowlevel = server._mcp_server  # noqa: SLF001 -- FastMCP has no way to declare an experimental capability
    options = lowlevel.create_initialization_options(experimental_capabilities={"claude/channel": {}})
    async with stdio_server() as (read_stream, write_stream), anyio.create_task_group() as group:
        runs = Runs(group, write_stream.clone())
        holder.append(runs)
        try:
            await lowlevel.run(read_stream, write_stream, options)
        finally:
            with anyio.CancelScope(shield=True):
                await runs.stop_all()
            group.cancel_scope.cancel()
            # The stdio writer runs until every clone of its stream is
            # closed, so an open one kept the server alive after its
            # client left -- measured, it never exited.
            await runs.push_stream.aclose()


def main() -> None:
    anyio.run(serve)


if __name__ == "__main__":
    main()
