"""`vivarium-mcp`: runs as tools, and what happens to them as channel events.

An MCP server over stdio. `start` launches a run as a child process with
`--break-on-failure` on; the other tools are the control socket's
operations and a query over `events.jsonl`. When the run pauses, fails a
phase or finishes, a `notifications/claude/channel` event is pushed, so
Claude Code hears about it without polling:

    <channel source="vivarium" run="vivarium-pytest-x1" event="paused" reason="after cases failed">...

**The run is a child process, never this one.** MCP's stdio transport
is this process's stdout, and a session writes to stdout -- the terminal
sink, and a phase's captured `print`. One stray line there corrupts the
protocol. So a run is `vivarium run ...` (or `python -m vivarium.cli run`
for a spec) with its output in `<out>/terminal.log`, and this process
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
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, TypedDict

import anyio
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCMessage, JSONRPCNotification
from vivarium.control import SOCKET, Op, Reply, WatchRefused, request, watch
from vivarium.journal import Tail
from vivarium.monitor import LIVE, channel_event, paused_now

from .cli import split_attr

if TYPE_CHECKING:
    from anyio.abc import TaskGroup
    from anyio.streams.memory import MemoryObjectSendStream

CHANNEL: Final = "notifications/claude/channel"

INSTRUCTIONS: Final = """\
Runs NixOS guests under vivarium and lets you reach into them.

`start` launches a run in the background and returns its id at once; it
pauses on the first failing phase with the guests still up. `exec` runs
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
one line each, and exits with the verdict (0 passed, 1 failed, 2 exited
without one), with 4 at a pause or 6 at `ready`. Run it again after each `resume`.
`monitor_all` prints every event, progress included, through pauses to
the verdict.
On `paused`, look with `events` and `exec` before you `resume` -- the
guests go down when the run ends. `stop` ends a run early and still
tears the guests down.
"""


# ── pure ────────────────────────────────────────────────────────────


class Event(TypedDict, total=False):
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
) -> list[Event]:
    """The last `limit` events that match every filter given."""
    found: list[Event] = []
    for line in lines:
        try:
            event: Event = json.loads(line)
        except json.JSONDecodeError:
            continue
        data = event.get("data") or {}
        if kind is not None and event.get("kind") != kind:
            continue
        if machine is not None and event.get("machine") != machine:
            continue
        if phase is not None and event.get("phase") != phase:
            continue
        if unit is not None and data.get("unit") != unit:
            continue
        if case is not None and case not in str(data.get("case", "")):
            continue
        if contains is not None and contains not in str(event.get("text", "")):
            continue
        found.append(event)
    return found[-limit:] if limit > 0 else found


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
    runner: Path | None = None,
    kernel: str | None = None,
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
    if hold:
        argv.append("--hold")
    if kernel is not None:
        argv += ["--kernel", kernel]
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


@dataclass
class Run:
    id: str
    out: Path
    process: Child
    source: Source | None = None
    finished: bool = False
    backlog: list[dict[str, str]] = field(default_factory=list)
    """Every event pushed for this run: `resume` reads whether a pause is open."""
    pushed: anyio.Event = field(default_factory=anyio.Event)
    """Set, and replaced, at each push: what `resume` waits on."""

    def took(self, event: dict[str, str]) -> None:
        self.backlog.append(event)
        self.pushed.set()
        self.pushed = anyio.Event()

    @property
    def socket(self) -> Path:
        return self.out / SOCKET


class Runs:
    """Every run this server started, and the push channel for them."""

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

    async def start(self, argv: list[str], out: Path, env: dict[str, str], source: Source | None = None) -> Run:
        log = (out / "terminal.log").open("wb")
        process = await anyio.open_process(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, **env},
        )
        log.close()
        run = Run(id=out.name, out=out, process=process, source=source)
        self.runs[run.id] = run
        self.group.start_soon(self._watch, run)
        return run

    async def _watch(self, run: Run) -> None:
        """Push what matters of the run's events, until it exits: from
        `watch` on its control socket, or from `events.jsonl` for a runner
        that serves none."""
        try:
            if await _bound(run):
                async for event in watch(run.socket):
                    if event == LIVE:
                        continue
                    run.finished = run.finished or event.get("event") == "finished"
                    meta = {key: value for key, value in event.items() if key != "text"}
                    await self.push(event.get("text", ""), meta)
        except WatchRefused:
            await self._tail(run)
        except OSError:
            # The run went away between binding its socket and our connect.
            pass
        await run.process.wait()
        if not run.finished:
            # Died before a verdict: an evaluation error, a crash. The
            # reason is at the end of what it printed.
            tail = why_it_exited(_read(run.out / "terminal.log"))
            await self.push(
                f"run exited {run.process.returncode} without a verdict:\n{tail}",
                {"run": run.id, "event": "exited"},
            )

    async def _tail(self, run: Run) -> None:
        """`_watch` for an older runner: polls `events.jsonl`, the only
        thing such a run offers."""
        tail = Tail(run.out / "events.jsonl")
        while True:
            for line in await tail.read():
                try:
                    event: dict[str, Any] = json.loads(line)
                except json.JSONDecodeError:
                    continue
                run.finished = run.finished or event.get("kind") == "run_finished"
                pushed = channel_event(event, run.id)
                if pushed is not None:
                    await self.push(*pushed)
            if run.process.returncode is not None:
                return
            await anyio.sleep(0.25)

    def get(self, run: str) -> Run:
        found = self.runs.get(run)
        if found is None:
            raise ValueError(f"no run {run!r}; have {', '.join(self.runs) or 'none'}")
        return found

    async def stop_all(self) -> None:
        for run in self.runs.values():
            await _stop(run)


async def _bound(run: Run) -> bool:
    """Whether the run binds its control socket before it exits.

    Polls: the child evaluates and builds first, then binds the socket,
    and says so nowhere a process can wait on."""
    socket = anyio.Path(run.socket)
    while run.process.returncode is None:
        if await socket.exists():
            return True
        await anyio.sleep(0.25)
    return False


def _directories(paths: list[str] | None) -> tuple[Path, ...]:
    return tuple(Path(path).absolute() for path in paths or [])


async def _stop(run: Run) -> None:
    """`ctl stop` through the socket, so the verdict says stopped; SIGINT
    when the run has no socket or does not know `stop`. The run answers
    either with its shielded teardown."""
    if run.process.returncode is not None:
        return
    asked = False
    # Bounded: a run that hangs on its socket still gets the signal.
    with anyio.move_on_after(10), contextlib.suppress(OSError):
        asked = (await request(run.socket, Op.STOP)).ok
    if not asked:
        run.process.send_signal(signal.SIGINT)
    with anyio.move_on_after(60):
        await run.process.wait()
        return
    run.process.kill()


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


def _reply(reply: Reply) -> dict[str, Any]:
    return {key: value for key, value in vars(reply).items() if value not in (None, "")}


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
        pytest_args: list[str] | None = None,
        env: dict[str, str] | None = None,
        kernel: str | None = None,
        cpus: int | dict[str, int] | None = None,
        cpu_scale: float | dict[str, float] | None = None,
        hold: bool = False,
    ) -> dict[str, str]:
        """Start a run in the background; returns its id at once.

        `attr` is evaluated from `file` (a directory means its default.nix),
        or give `spec`, a spec path. `breaks` pauses before those phases,
        or at `start`, `failure` or `end`;
        `break_on_failure` pauses on a failed phase. `env` is added to the
        run's environment, which the evaluation reads: a knob's variable,
        or `UMBRELLA_DEV` to build against a working copy.
        `kernel` boots a kernel from a working tree instead of Nix's:
        `linux` from a UML build, or a bzImage with virtio built in.
        `cpus` is a processor count, `cpu_scale` a share of the host's
        cores (0.5 is half); a number is every guest, `{"db": 8}` one
        guest. Either replaces `vivarium.cpus` for this run.
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
            file=os.path.abspath(file),
            breaks=breaks or [],
            break_on_failure=break_on_failure,
            only=only or [],
            offline=offline,
            pytest_args=pytest_args or [],
            runner=Path(written["vivarium"]) if written.get("vivarium") else None,
            kernel=os.path.abspath(kernel) if kernel else None,
            cpus=cpus,
            cpu_scale=cpu_scale,
            hold=hold,
        )
        source = Source(attr=attr, file=os.path.abspath(file), env=env or {}) if attr is not None else None
        run = await runs().start(argv, out, env or {}, source)
        every = [sys.executable, "-m", "vivarium.cli", "monitor", str(out)]
        return {
            "run": run.id,
            "out": str(out),
            "monitor": shlex.join([*every, "--quiet"]),
            "monitor_all": shlex.join([*every, "--through-pauses"]),
        }

    @server.tool()
    async def state(run: str) -> dict[str, Any]:
        """Each phase's state, and whether the run is paused, running or gone."""
        found = runs().get(run)
        if found.process.returncode is not None:
            return {"run": run, "status": f"exited {found.process.returncode}"}
        if not found.socket.exists():
            return {"run": run, "status": "starting or running without a control socket"}
        return _reply(await request(found.socket, Op.STATE))

    @server.tool(name="exec")
    async def exec_(run: str, code: str) -> dict[str, Any]:
        """Run Python in the run, paused or running. Top-level await;
        `vms`, `session` and each guest by name are in scope; names
        persist between calls. The last expression's repr is `result`."""
        return _reply(await request(runs().get(run).socket, Op.EXEC, code))

    @server.tool()
    async def inject(run: str, path: str, reload: list[str] | None = None) -> dict[str, Any]:
        """Run a local file's `async def test(vms)` in the run, paused or
        running, read fresh from disk, so an edit takes effect by
        injecting it again. `reload` names working-tree directories of
        helper modules: imported from first, their modules dropped, so an
        edited helper is read again too."""
        return _reply(
            await request(runs().get(run).socket, Op.INJECT, os.path.abspath(path), _directories(reload))
        )

    @server.tool()
    async def run_pytest(
        run: str, path: str, args: list[str] | None = None, reload: list[str] | None = None
    ) -> dict[str, Any]:
        """Run pytest on a local test file or directory against the paused
        guests, read fresh from disk: edit a test and run it again without
        rebuilding the setup. `args` go to pytest (`["-k", "etcd"]`). The
        cases are events marked by_hand and do not count toward the run's
        verdict. `reload` is as for `inject`."""
        arg = shlex.join([os.path.abspath(path), *(args or [])])
        return _reply(await request(runs().get(run).socket, Op.PYTEST, arg, _directories(reload)))

    @server.tool()
    async def run_phase(run: str, phase: str) -> dict[str, Any]:
        """Run a declared phase now, in the paused run."""
        return _reply(await request(runs().get(run).socket, Op.RUN, phase))

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
        return _reply(await request(runs().get(run).socket, Op.SEGMENT, arg))

    @server.tool()
    async def resume(run: str) -> dict[str, Any]:
        """Continue a paused run."""
        found = runs().get(run)
        reply = _reply(await request(found.socket, Op.CONTINUE))
        # Until the pause is resolved in the backlog: a `monitor`
        # armed on this reply would otherwise replay the pause and exit.
        with anyio.move_on_after(10):
            while paused_now(found.backlog) and found.process.returncode is None:
                await found.pushed.wait()
        return reply

    @server.tool()
    async def switch(run: str, node: str, module: str, action: str = "switch") -> dict[str, Any]:
        """Add a NixOS module file to one guest of a running session and
        switch it: the host evaluates the session again with the module on
        `node`, builds it, and the guest switches to the result. `action` is
        switch-to-configuration's: switch, test, boot or dry-activate. Only
        for a run started by `attr`; edit the file and call again to iterate."""
        found = runs().get(run)
        if found.source is None:
            return {"ok": False, "error": "this run was started from a spec; switch needs one started by attr"}
        argv = [
            str(Path(sys.executable).parent / "vivarium"),
            "switch",
            found.source.attr,
            "--file",
            found.source.file,
            "--out",
            str(found.out),
            "--node",
            node,
            "--module",
            os.path.abspath(module),
            "--action",
            action,
        ]
        done = await anyio.run_process(
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            env={**os.environ, **found.source.env},
        )
        return {"ok": done.returncode == 0, "output": done.stdout.decode(errors="replace")}

    @server.tool()
    async def reload(run: str) -> dict[str, Any]:
        """Evaluate a paused session again and give the run its edited
        phases: scripts, pytest specs, `after`, `nodes`, settings. The guests
        stay; a change to a guest is refused (use `switch`). A phase that ran
        keeps its verdict until `run_phase` runs it again. Only for a run
        started by `attr`."""
        found = runs().get(run)
        if found.source is None:
            return {"ok": False, "error": "this run was started from a spec; reload needs one started by attr"}
        argv = [
            str(Path(sys.executable).parent / "vivarium"),
            "reload",
            found.source.attr,
            "--file",
            found.source.file,
            "--out",
            str(found.out),
        ]
        done = await anyio.run_process(
            argv,
            check=False,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            env={**os.environ, **found.source.env},
        )
        return {"ok": done.returncode == 0, "output": done.stdout.decode(errors="replace")}

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
        reply = await request(runs().get(run).socket, Op.EXEC, code)
        lines = (reply.output or "").strip().splitlines()
        if not reply.ok or not lines:
            return [json.dumps(_reply(reply))]
        path = Path(lines[-1])
        return [Image(data=path.read_bytes(), format="png"), str(path)]

    @server.tool()
    async def stop(run: str) -> dict[str, Any]:
        """End a run now. The evidence is written and the guests go down."""
        found = runs().get(run)
        await _stop(found)
        return {"run": run, "status": f"exited {found.process.returncode}"}

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
    ) -> dict[str, list[Event]]:
        """The run's events, newest last, filtered. `kind="journal", machine="cp",
        unit="kubelet.service"` is one service on one guest; `kind="case"` is
        each pytest test; `kind="error"` is every traceback."""
        path = runs().get(run).out / "events.jsonl"
        try:
            lines = path.read_text(errors="replace").splitlines()
        except OSError:
            return {"events": []}
        return {
            "events": select(
                lines,
                kind=kind,
                machine=machine,
                unit=unit,
                phase=phase,
                case=case,
                contains=contains,
                limit=limit,
            )
        }

    # Every tool returns an object. FastMCP wraps any other return type
    # as `{"result": ...}`, and `exec`'s own reply has a `result` key, so
    # a client could not tell the two apart.
    @server.tool(name="runs")
    async def list_runs() -> dict[str, list[dict[str, str]]]:
        """Every run this server started."""
        return {
            "runs": [
                {
                    "run": found.id,
                    "out": str(found.out),
                    "status": "running" if found.process.returncode is None else f"exited {found.process.returncode}",
                }
                for found in runs().runs.values()
            ]
        }

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
