"""Reaching into a live session: breakpoints and injected Python.

Changing a phase in Nix costs an evaluation and a new store path, and
changing a guest costs a new image. So the fast loop does neither: the
run pauses at a breakpoint with the guests up, and Python is sent into
it. `exec` runs code with top-level `await`, `inject` runs a file from
the working tree, `pytest` runs tests from it, `run` runs a declared
phase, `continue` resumes. `exec` and `inject` also reach a run that is
not paused, beside the phases that are running.

    vivarium ctl --out o exec 'await one.succeed("systemctl --failed")'
    vivarium ctl --out o inject ./scratch.py
    vivarium ctl --out o pytest ./tests/chaos -- -k etcd
    vivarium ctl --out o continue

The operations are the MCP server's tools as well; it is one more
client of `<out>/control.sock`.

**The socket runs arbitrary code as the user who started the run.** It
is created mode 0600 in the output directory. A sandboxed check runs
with `--no-control` and has none.

Every operation is an event, code included, so `events.jsonl` says
what was done to the guests by hand and not only what the phases did.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import io
import json
import os
import shlex
import sys
import traceback
from dataclasses import asdict, dataclass, fields
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

import anyio

from .events import Kind, Level
from .phases import PhaseState, Stop
from .session import CasesFailed, load_phase
from .spec import PytestSpec

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from contextlib import AbstractContextManager
    from types import ModuleType

    from anyio.abc import ByteStream, TaskStatus

    from .session import Session

SOCKET: Final = "control.sock"
RUN_FILE: Final = "run.json"


class Op(StrEnum):
    STATE = "state"
    EXEC = "exec"
    INJECT = "inject"
    RUN = "run"
    PYTEST = "pytest"
    CONTINUE = "continue"
    STOP = "stop"


@dataclass
class Reply:
    ok: bool
    output: str = ""
    result: str | None = None
    error: str | None = None
    state: dict[str, str] | None = None
    pause: str | None = None
    """Why the run is paused, as `state` answers it: `before later`."""
    errors: dict[str, str] | None = None
    """Each failed phase's error, as `state` answers it."""

    def encode(self) -> bytes:
        return json.dumps(asdict(self)).encode() + b"\n"

    @classmethod
    def decode(cls, line: bytes) -> Reply:
        """A reply from a run of any vivarium. The CLI on PATH and the
        runner of a run are built apart, so a field this one does not know
        is dropped, not an error."""
        raw = json.loads(line)
        known = {f.name for f in fields(cls)}
        return cls(**{key: value for key, value in raw.items() if key in known})


# ── pure ────────────────────────────────────────────────────────────


def split_last_expression(source: str) -> tuple[ast.Module, ast.Expression | None]:
    """The statements, and the final expression if the code ends in one.

    What a REPL does: `await one.succeed("hostname")` should answer with
    the hostname, not with nothing, so a trailing expression is split off
    and evaluated for its value.
    """
    tree = ast.parse(source, mode="exec")
    if tree.body and isinstance(last := tree.body[-1], ast.Expr):
        tree.body.pop()
        return tree, ast.Expression(body=last.value)
    return tree, None


@dataclass(frozen=True, slots=True)
class Request:
    """One request on the socket."""

    op: Op
    arg: str = ""
    reload: tuple[Path, ...] = ()
    """`inject` and `pytest`: working-tree directories to import from
    first, their modules dropped. Absent from an older client's request."""

    def encode(self) -> bytes:
        document: dict[str, object] = {"op": str(self.op), "arg": self.arg}
        if self.reload:
            document["reload"] = [str(path) for path in self.reload]
        return json.dumps(document).encode() + b"\n"


RELOADS: Final = frozenset({Op.INJECT, Op.PYTEST})


def parse_request(line: bytes) -> Request:
    """One request: `{"op": ..., "arg": ..., "reload": [...]}`. Raises ValueError."""
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as error:
        raise ValueError(f"not JSON: {error}") from None
    if not isinstance(raw, dict):
        raise ValueError("a request is a JSON object")
    try:
        op = Op(raw.get("op"))
    except ValueError:
        raise ValueError(f"no such op {raw.get('op')!r}; have {', '.join(Op)}") from None
    arg = raw.get("arg", "")
    if not isinstance(arg, str):
        raise ValueError("arg is a string")
    if op in (Op.EXEC, Op.INJECT, Op.RUN, Op.PYTEST) and not arg:
        raise ValueError(f"{op} needs an arg")
    reload = raw.get("reload", [])
    if not isinstance(reload, list) or not all(isinstance(path, str) and Path(path).is_absolute() for path in reload):
        raise ValueError("reload is a list of absolute directories")
    if reload and op not in RELOADS:
        raise ValueError(f"reload is for {' and '.join(sorted(RELOADS))}, not {op}")
    return Request(op, arg, tuple(Path(path) for path in reload))


def provided(directory: Path) -> set[str]:
    """The top-level modules a directory on `sys.path` provides."""
    modules = {path.stem for path in directory.glob("*.py")}
    packages = {path.parent.name for path in directory.glob("*/__init__.py")}
    return modules | packages


def forget(names: set[str], modules: dict[str, ModuleType]) -> list[str]:
    """Drop each named module and its submodules from `modules`; the names
    dropped, sorted."""
    dropped = sorted(key for key in modules if key in names or key.split(".", 1)[0] in names)
    for key in dropped:
        del modules[key]
    return dropped


# ── the console ─────────────────────────────────────────────────────


class Console:
    """Python run against the session, one namespace for all of it.

    A name bound by one `exec` is there for the next, the way a REPL
    keeps its variables: finding a pid in one call and killing it in the
    next is the ordinary shape of poking at a failure.
    """

    def __init__(
        self,
        namespace: dict[str, Any],
        capture: Callable[[io.StringIO], AbstractContextManager[object]] = contextlib.redirect_stdout,
    ) -> None:
        self.namespace = namespace
        # What sends a print to the reply. The session's, in a run: see
        # `Session._capture`.
        self.capture = capture

    async def execute(self, source: str) -> Reply:
        output = io.StringIO()
        flags = ast.PyCF_ALLOW_TOP_LEVEL_AWAIT
        try:
            body, last = split_last_expression(source)
            with self.capture(output):
                await _run(compile(body, "<uml ctl>", "exec", flags=flags), self.namespace)
                result = None
                if last is not None:
                    result = await _run(compile(last, "<uml ctl>", "eval", flags=flags), self.namespace)
        except Exception:  # noqa: BLE001 -- the caller's code; its failure is the reply
            return Reply(ok=False, output=output.getvalue(), error=traceback.format_exc())
        return Reply(
            ok=True,
            output=output.getvalue(),
            result=None if result is None else repr(result),
        )


async def _run(code: Any, namespace: dict[str, Any]) -> Any:
    # With top-level await allowed, `eval` hands back a coroutine when the
    # code awaited anything, and the value itself when it did not.
    value = eval(code, namespace)  # noqa: S307 -- running the caller's code is the feature
    if code.co_flags & inspect.CO_COROUTINE:
        value = await value
    return value


@contextlib.contextmanager
def reachable(path: Path) -> Iterator[str]:
    """A name for the socket at `path` that fits in `sun_path`, 108 bytes.

    `--out` can be deeper than that. `/proc/self/fd/<directory>/<name>` is
    the same file under a short name, and is needed only to bind and to
    connect: unlink and chmod take any length.
    """
    if len(os.fsencode(path)) < 100:
        yield str(path)
        return
    directory = os.open(path.parent, os.O_PATH | os.O_DIRECTORY)
    try:
        yield f"/proc/self/fd/{directory}/{path.name}"
    finally:
        os.close(directory)


async def _receive_line(stream: ByteStream) -> bytes:
    buffer = b""
    while b"\n" not in buffer:
        try:
            buffer += await stream.receive()
        except anyio.EndOfStream:
            break
    return buffer.split(b"\n", 1)[0]


# ── the controller ──────────────────────────────────────────────────


class Use(StrEnum):
    """What a printed command is for: the key `run.json` is read by."""

    MONITOR = "monitor"
    EXEC = "exec"
    INJECT = "inject"
    STATE = "state"
    CONTINUE = "continue"
    SWITCH = "switch"


ORIGIN: Final = "VIVARIUM_ORIGIN"
"""The environment variable the `vivarium` CLI sets for a run it starts."""


@dataclass(frozen=True, slots=True)
class Origin:
    """How the `vivarium` CLI started a run: what `vivarium switch` needs to
    evaluate it again. Only a run started by attribute has one."""

    cli: Path
    attr: str
    file: Path

    def to_json(self) -> str:
        return json.dumps({"cli": str(self.cli), "attr": self.attr, "file": str(self.file)})

    @classmethod
    def from_json(cls, text: str) -> Origin:
        """Raises ValueError on anything but the three absolute fields."""
        raw = json.loads(text)
        if not isinstance(raw, dict) or not all(isinstance(raw.get(key), str) for key in ("cli", "attr", "file")):
            raise ValueError(f"{ORIGIN} is not an origin: {text!r}")
        origin = cls(Path(raw["cli"]), raw["attr"], Path(raw["file"]))
        if not (origin.cli.is_absolute() and origin.file.is_absolute()):
            raise ValueError(f"{ORIGIN} names a relative path: {text!r}")
        return origin


@dataclass(frozen=True, slots=True)
class Command:
    """One command line that reaches a run, kept as argv until printed."""

    use: Use
    what: str
    argv: tuple[str, ...]

    @property
    def line(self) -> str:
        return shlex.join(self.argv)

    @property
    def needs_socket(self) -> bool:
        """Every command but the monitor talks to `control.sock`; the
        monitor also follows `events.jsonl`."""
        return self.use is not Use.MONITOR


def commands(vivarium: Path, out: Path, origin: Origin | None = None, node: str | None = None) -> list[Command]:
    """Each command that reaches the run in `out`, with what it does.

    The run prints them, so an agent that reads the run's output can copy
    one as it is: absolute paths, no PATH, no knowledge of vivarium.
    `switch` only with an origin: a run by `nix run` cannot be evaluated
    again, and a line with placeholders is not one to copy."""
    ctl = (str(vivarium), "ctl", "--out", str(out))
    switch: list[Command] = []
    if origin is not None and node is not None:
        argv = (str(origin.cli), "switch", origin.attr, "--file", str(origin.file), "--out", str(out))
        switch = [
            Command(Use.SWITCH, f"add a module to {node} and switch it", (*argv, "--node", node, "--module", "./module.nix"))
        ]
    return [
        Command(
            Use.MONITOR,
            "wait for a pause or the verdict (exits 4 at a pause)",
            (str(vivarium), "monitor", str(out), "--quiet"),
        ),
        Command(Use.EXEC, "run Python against the guests", (*ctl, "exec", "print(list(vms))")),
        Command(Use.INJECT, "run a file's `async def test(vms)`", (*ctl, "inject", "./check.py")),
        Command(Use.STATE, "each phase's state", (*ctl, "state")),
        Command(Use.CONTINUE, "resume a paused run", (*ctl, "continue")),
        *switch,
    ]


@dataclass(frozen=True, slots=True)
class RunFile:
    """`<out>/run.json`: how to reach a run, written before its first guest boots.

    A caller reads this instead of the run's output. Static on purpose:
    the run's progress is in `phases.json` and `ctl state`."""

    pid: int
    out: Path
    vivarium: Path
    socket: Path | None
    """`None` for a run with no control socket (`--no-control`)."""
    commands: tuple[Command, ...]
    origin: Origin | None = None

    def command(self, use: Use) -> Command | None:
        return next((c for c in self.commands if c.use is use), None)

    def to_json(self) -> str:
        document = {
            "pid": self.pid,
            "out": str(self.out),
            "vivarium": str(self.vivarium),
            "socket": None if self.socket is None else str(self.socket),
            # `command` beside `argv` for a shell reader: `jq -r` gives a line to paste.
            "commands": [
                {"use": str(c.use), "what": c.what, "argv": list(c.argv), "command": c.line} for c in self.commands
            ],
            "origin": None if self.origin is None else json.loads(self.origin.to_json()),
        }
        return json.dumps(document, indent=2) + "\n"

    @classmethod
    def from_json(cls, text: str) -> RunFile:
        raw = json.loads(text)
        return cls(
            pid=int(raw["pid"]),
            out=Path(raw["out"]),
            vivarium=Path(raw["vivarium"]),
            socket=None if raw["socket"] is None else Path(raw["socket"]),
            commands=tuple(Command(Use(c["use"]), str(c["what"]), tuple(c["argv"])) for c in raw["commands"]),
            origin=None if raw.get("origin") is None else Origin.from_json(json.dumps(raw["origin"])),
        )


def run_file(
    vivarium: Path, out: Path, pid: int, *, control: bool, origin: Origin | None = None, node: str | None = None
) -> RunFile:
    """The run file of a run in `out`; with no socket, only what works without one."""
    return RunFile(
        pid=pid,
        out=out,
        vivarium=vivarium,
        socket=out / SOCKET if control else None,
        commands=tuple(c for c in commands(vivarium, out, origin, node) if control or not c.needs_socket),
        origin=origin,
    )


def first_guest(session: Session) -> str | None:
    """The guest a printed `switch` names; the caller edits it as needed."""
    return next((str(machine["name"]) for machine in session.spec.machines), None)


def vivarium_command(session: Session) -> Path:
    """The runner executable of this run: the printed commands use it, so
    a command always speaks the socket protocol of the run it reaches."""
    if session.spec.vivarium is not None:
        return session.spec.vivarium / "bin" / "vivarium-runner"
    return Path(sys.argv[0]).resolve()


class Controller:
    """Serves the socket for a whole drive; pauses when told to.

    `exec` and `inject` run at any time, beside the running phases: the
    caller chose to touch those guests, and prints go to the reply by
    task, not by swapping `sys.stdout`. `run` and `pytest` wait for a
    pause, because the scheduler owns the phases and pytest is not
    reentrant.
    """

    ANY_TIME: Final = frozenset({Op.STATE, Op.EXEC, Op.INJECT, Op.STOP})

    def __init__(
        self, session: Session, stop: Callable[[], None] | None = None, origin: Origin | None = None
    ) -> None:
        """`stop` cancels the drive; without it, `stop` is refused. `origin`
        adds `switch` to the printed commands."""
        self.session = session
        self.origin = origin
        self._stop = stop
        self._stopping = False
        self.path = session.out / SOCKET
        self.console = Console(self._namespace(), lambda copy: session._capture("exec", copy))
        self._resume: anyio.Event | None = None
        self._pause: str | None = None
        self._continuing = False

    def _namespace(self) -> dict[str, Any]:
        return {"session": self.session, "anyio": anyio}

    @property
    def paused(self) -> bool:
        return self._resume is not None

    async def serve(self, *, task_status: TaskStatus[None] = anyio.TASK_STATUS_IGNORED) -> None:
        """Listen until cancelled. Run beside the drive, like `follow`.

        Started with `task_group.start`, so the socket exists before the
        first guest boots: a client that sees the pause message can
        always connect.
        """
        self.session.out.mkdir(parents=True, exist_ok=True)
        self.path.unlink(missing_ok=True)
        with reachable(self.path) as name:
            listener = await anyio.create_unix_listener(name)
        os.chmod(self.path, 0o600)
        task_status.started()
        try:
            await listener.serve(self._client)
        finally:
            with anyio.CancelScope(shield=True):
                await listener.aclose()
            self.path.unlink(missing_ok=True)

    async def pause(self, reason: str) -> None:
        """Hold the drive here until a client says `continue`."""
        self._resume = anyio.Event()
        self._pause = reason
        self._bind_guests()
        self.session.emit(
            Kind.NOTE,
            f"paused {reason}; the guests stay up until the run is resumed",
            level=Level.ERROR,
            reason=reason,
            socket=str(self.path),
        )
        # At the pause's level, so `--quiet` shows them when they matter.
        self.announce(Level.ERROR)
        try:
            await self._resume.wait()
        finally:
            self._resume = None
            self._pause = None
        self.session.emit(Kind.NOTE, f"resumed, paused {reason}", resumed=reason)

    def announce(self, level: Level = Level.INFO) -> None:
        """The command lines that reach this run, one event each."""
        out = self.session.out.resolve()
        for command in commands(vivarium_command(self.session), out, self.origin, first_guest(self.session)):
            self.session.emit(Kind.NOTE, f"{command.what}: {command.line}", level=level, command=command.what)

    def _bind_guests(self) -> None:
        """`vms` and each guest by name, once the guests exist."""
        if self.session.vms is not None:
            self.console.namespace.setdefault("vms", self.session.vms)
            for name, vm in self.session.vms.items():
                self.console.namespace.setdefault(name, vm)

    async def _client(self, stream: ByteStream) -> None:
        async with stream:
            line = await _receive_line(stream)
            if not line:
                return
            reply = await self.handle(line)
            await stream.send(reply.encode())
        # After the reply, not in `handle`: resuming lets the drive
        # finish, and a finished drive cancels this server -- measured,
        # the client of a `continue` got no reply at all.
        if self._continuing and self._resume is not None:
            self._continuing = False
            self._resume.set()
        if self._stopping and self._stop is not None:
            self._stopping = False
            self.session.stopped = Stop.by_request()
            self.session.emit(Kind.NOTE, "ctl stop: tearing the guests down")
            self._stop()

    async def handle(self, line: bytes) -> Reply:
        try:
            asked = parse_request(line)
        except ValueError as error:
            return Reply(ok=False, error=str(error))
        op, arg = asked.op, asked.arg
        self.session.emit(Kind.NOTE, f"control: {op} {arg}".rstrip(), level=Level.DETAIL, op=str(op), arg=arg)
        if op is Op.STATE:
            return Reply(
                ok=True,
                state=self._state(),
                result="paused" if self.paused else "running",
                pause=self._pause,
                errors=dict(self.session.errors) or None,
            )
        if op is Op.STOP:
            if self._stop is None:
                return Reply(ok=False, error="this run cannot be stopped through its socket")
            self._stopping = True
            return Reply(ok=True, result="stopping")
        if op not in self.ANY_TIME and not self.paused:
            return Reply(ok=False, error=f"{op} only while paused; the run is running")
        if op is Op.INJECT and self.session.vms is None:
            return Reply(ok=False, error="no guests are up yet")
        self._bind_guests()
        if op is Op.CONTINUE:
            self._continuing = True
            return Reply(ok=True)
        if op is Op.EXEC:
            return self._record("exec", await self.console.execute(arg))
        if op is Op.INJECT:
            self._reload(asked.reload)
            return self._record(f"inject:{Path(arg).name}", await self._inject(arg))
        if op is Op.PYTEST:
            self._reload(asked.reload)
            return await self._pytest(arg)
        return await self._run(arg)

    def _record(self, source: str, reply: Reply) -> Reply:
        """What injected code raised, into the event stream as well as
        the reply: the record of a run includes what was done to it by
        hand. Its prints are events already, through `Session._capture`."""
        if reply.error:
            self.session.emit(Kind.ERROR, reply.error.rstrip(), level=Level.ERROR, phase=source)
        return reply

    def _reload(self, directories: tuple[Path, ...]) -> None:
        """Each directory first on `sys.path`, and the modules it provides
        dropped, so the next import reads the working tree and not the
        store copy the run imported. nixidae/nixidae#851."""
        for directory in directories:
            dropped = forget(provided(directory), sys.modules)
            if str(directory) in sys.path:
                sys.path.remove(str(directory))
            sys.path.insert(0, str(directory))
            self.session.emit(
                Kind.NOTE,
                f"reload {directory}: dropped {', '.join(dropped) or 'nothing'}",
                level=Level.DETAIL,
                reload=str(directory),
                dropped=dropped,
            )

    async def _inject(self, arg: str) -> Reply:
        """A file's `test(vms)`, read from where it is, not from the store.

        Imported fresh each time, so an edit is picked up by sending it
        again. Its outcome is the reply; the run's verdict is untouched.
        """
        output = io.StringIO()
        try:
            test = load_phase(Path(arg).expanduser().resolve())
            vms = self.session.vms
            if vms is None:
                return Reply(ok=False, error="no guests are up yet")
            with self.session._capture(f"inject:{Path(arg).name}", output):
                await test(vms)
        except Exception:  # noqa: BLE001 -- the injected code's failure is the reply
            return Reply(ok=False, output=output.getvalue(), error=traceback.format_exc())
        return Reply(ok=True, output=output.getvalue())

    async def _pytest(self, arg: str) -> Reply:
        """pytest from the working tree against the paused guests.

        What `inject` is for a script: the loop for a pytest phase is
        editing a test and sending it again, against guests a long setup
        already built. `arg` is `PATH [PYTEST ARGS...]`, shell-quoted. The
        cases are events marked `by_hand`, and the run's verdict and
        junit.xml leave them out.
        """
        path, *args = shlex.split(arg)
        tests = Path(path).expanduser().resolve()
        if not tests.exists():
            return Reply(ok=False, error=f"no tests at {tests}")
        if self.session.vms is None:
            return Reply(ok=False, error="no guests are up")
        name = f"pytest:{tests.name}"
        spec = PytestSpec(tests=tests, args=args)
        try:
            summary = await self.session._pytest(name, spec, self.session.vms, by_hand=True)
        except CasesFailed as error:
            return Reply(ok=False, result="failed", error=str(error))
        except Exception:  # noqa: BLE001 -- the tests' failure to load is the reply
            return self._record(name, Reply(ok=False, error=traceback.format_exc()))
        return Reply(ok=True, result=summary)

    async def _run(self, name: str) -> Reply:
        phase = next((p for p in self.session.spec.phases if p.name == name), None)
        if phase is None:
            return Reply(ok=False, error=f"no phase {name!r}")
        state = await self.session.run(phase)
        return Reply(
            ok=state is PhaseState.PASSED,
            result=str(state),
            error=self.session.errors.get(name),
            state=self._state(),
        )

    def _state(self) -> dict[str, str]:
        return {name: str(state) for name, state in self.session.state.items()}

    # ── what `vivarium run --interactive` calls, in the same process ─────

    async def execute(self, source: str) -> Reply:
        """`exec`, from the REPL rather than the socket."""
        self._bind_guests()
        return self._record("exec", await self.console.execute(source))

    async def run_phase(self, name: str) -> str:
        """Run one declared phase; its state. Called from REPL code, which
        runs while the drive is paused."""
        reply = await self._run(name)
        if reply.error:
            print(reply.error)
        return reply.result or ""

    def resume(self) -> None:
        """Let the remaining phases run, once the current input returns."""
        if self._resume is not None:
            self._resume.set()


# ── the client ──────────────────────────────────────────────────────


async def request(socket: Path, op: Op, arg: str = "", reload: tuple[Path, ...] = ()) -> Reply:
    """One request, one reply. What `vivarium ctl` and a test both use."""
    with reachable(socket) as name:
        connected = await anyio.connect_unix(name)
    async with connected as stream:
        await stream.send(Request(op, arg, reload).encode())
        line = await _receive_line(stream)
    if not line:
        return Reply(ok=False, error="the run closed the connection without a reply")
    return Reply.decode(line)
