"""`vivarium`: the CLI a person or an agent runs, built on huggorm.

    vivarium run checks.pytest-phase --out ./out -- -k hostname
    vivarium phases checks.recipes --file ~/Code/myproject
    vivarium switch --out ./out --node one --module ./more.nix
    vivarium reload --out ./out
    vivarium ctl --out ./out exec 'print(list(vms))'
    vivarium monitor ./out --quiet

`run` is `nix run --file . <attr>.driver` with the evaluation and the
build moved inside it: huggorm evaluates `--file`, selects the
attribute, builds its `.driver` (which pulls in the spec and the phase
type check) and execs it. There is no `nix build` first and no store
path to paste. `ctl` and `monitor` are the runner's own, from its
library: the socket protocol is JSON, so they reach a run of another
vivarium too.

**The runner is a separate program, `vivarium-runner`, on purpose.**
huggorm links Nix, and the runner is what every sandboxed check runs. A
sandboxed check must not evaluate -- everything is decided by the time
it runs -- so it never needs this, and a consumer of `mkTest` never
builds it to run a test with `nix build` or `nix run`.

Evaluation is impure, as `nix build --file` is, so a knob reads the
environment the same way here as there.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import subprocess
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal, NamedTuple

import anyio
import huggorm
from huggorm.attrpath import AttrPathError, auto_call, select_attr_path
from huggorm.errors import NixError
from huggorm_bindings import DerivedPathBuilt, OutputsSpec
from vivarium import access, cli as runner_cli, control, monitor, registry
from vivarium.phases import STOPPED

from .build_log import FILE as BUILD_LOG, SHOWN, Recorder

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from huggorm import AsyncEvalState, AsyncStore, AsyncValue


@dataclass(frozen=True)
class Request:
    command: str
    file: Path
    attr: list[str]
    rest: list[str]
    """Everything else, for `vivarium` itself: its options, and `--` with
    pytest's arguments after it."""
    out: Path | None = None
    """The runner's `--out`, read and left in `rest`: the build log and the
    first `run.json` go there."""
    until_pause: bool = False


def split_attr(text: str) -> list[str]:
    """`lan.qemu` to `["lan", "qemu"]`. No quoting: no attribute here
    has a dot in its name."""
    parts = text.split(".")
    if not all(parts):
        raise ValueError(f"not an attribute path: {text!r}")
    return parts


def entry(file: Path) -> Path:
    """The file to evaluate. A directory means its `default.nix`, as
    `nix build --file` takes it."""
    return file / "default.nix" if file.is_dir() else file


EVALUATE: Final = frozenset({"run", "phases", "up"})
"""The commands that evaluate an attribute and exec what it builds."""


def parser() -> argparse.ArgumentParser:
    """Every command. `ctl` and `monitor` come from the runner's parser,
    so the two programs take them the same way."""
    top = argparse.ArgumentParser(
        prog="vivarium",
        description="Evaluate a run, build it and run it; reach into a run; follow it",
        epilog="run and phases hand anything else to the runner: `vivarium run x --out o -v -- -k name`.",
    )
    sub = top.add_subparsers(dest="command", required=True)
    for command, what in (
        ("run", "evaluate, build and run a session"),
        ("phases", "list a session's phases"),
        ("up", "bring a session up as an environment: run its phases, return once it is ready, keep the guests up"),
    ):
        evaluate = sub.add_parser(command, help=what)
        evaluate.add_argument("attr", help="the attribute to run, such as `lan` or `lan.qemu`")
        evaluate.add_argument(
            "--file",
            "-f",
            type=Path,
            default=Path("."),
            help="the file or directory to evaluate, as `nix build --file` takes it",
        )
        if command == "run":
            evaluate.add_argument(
                "--until-pause",
                action="store_true",
                help=(
                    "start the run in the background, its output in <out>/run.log; wait as"
                    " `monitor --quiet` does and exit with its status (0 passed, 1 failed,"
                    " 2 no run, 4 paused, 5 stopped). The run goes on after a pause"
                ),
            )
    down = sub.add_parser("down", help="stop an environment `up` started; exit 0 if its phases passed")
    down.add_argument("--out", type=Path, required=True, help="the environment's --out")
    listing = sub.add_parser("ls", help="the runs up on this host: their state, test and --out")
    listing.add_argument("--json", action="store_true", help="one JSON object per run")
    add_switch(sub)
    add_reload(sub)
    runner_cli.add_ctl(sub)
    runner_cli.add_monitor(sub)
    return top


def split_dashdash(argv: list[str]) -> tuple[list[str], list[str]]:
    """Before `--`, and from it on. argparse drops `--`, and the runner
    needs it to know where pytest's arguments begin."""
    if "--" not in argv:
        return argv, []
    at = argv.index("--")
    return argv[:at], argv[at:]


def parse(argv: list[str]) -> Request:
    """`run` or `phases`: our two options, and the rest untouched for the runner."""
    head, tail = split_dashdash(argv)
    top = parser()
    args, rest = top.parse_known_args(head)
    if args.command not in EVALUATE:
        top.error(f"{args.command} does not evaluate")
    try:
        attr = split_attr(args.attr)
    except ValueError as error:
        top.error(str(error))
    peek = argparse.ArgumentParser(add_help=False)
    peek.add_argument("--out", type=Path)
    out = peek.parse_known_args(rest)[0].out
    return Request(
        args.command,
        args.file,
        attr,
        [*rest, *tail],
        None if out is None else out.absolute(),
        until_pause=args.command == "up" or (args.command == "run" and args.until_pause),
    )


async def resolve(file: Path, attr: list[str], command: str, recorder: Recorder) -> str:
    """Evaluate, build the attribute's `.run` or `.phases`, and return
    the program in it.

    That program, and not this package's `vivarium`, is what runs: it carries
    the `vivarium` of the library that was evaluated, which knows every field
    of the spec. Building `.run` also runs the type check of every phase
    script, exactly as `nix run --file . <attr>.run` does.
    """
    async with evaluator(recorder) as (state, store):
        target = await session_of(state, file, attr)
        # `vivarium run` runs the session's `.driver`; `phases` its `.phases`.
        member = await select_attr_path(state, target, [{"run": "driver"}.get(command, command)])
        if command == "run":
            # `drv_path` reads an attribute set; a selected attribute is a thunk until forced.
            await state.force(member)
            missing = await store.query_missing([DerivedPathBuilt(await member.drv_path(), OutputsSpec(all=True))])
            recorder.planned(
                [str(path) for path in missing.will_build()], [str(path) for path in missing.will_substitute()]
            )
        built = Path(await member.realise_string())
    [program] = sorted((built / "bin").iterdir())
    return str(program)


class Evaluator(NamedTuple):
    state: AsyncEvalState
    store: AsyncStore


@asynccontextmanager
async def evaluator(recorder: Recorder) -> AsyncIterator[Evaluator]:
    """An evaluator on the ambient store, with nix.conf read as `nix` reads
    it. What Nix says while the block runs goes to `recorder`: the
    evaluator's own records, and the builds' and fetches', which no
    evaluator's thread claims."""
    huggorm.load_config()
    async with huggorm.AsyncSession() as session:
        store = session.store()
        state = session.eval(store)
        async with (
            session.forward(state, recorder.take, level=int(SHOWN)),
            session.forward_process(recorder.take, level=int(SHOWN)),
        ):
            yield Evaluator(state, store)


async def session_of(state: AsyncEvalState, file: Path, attr: list[str]) -> AsyncValue:
    target = await auto_call(state, await state.eval_file(str(entry(file).resolve())))
    target = await select_attr_path(state, target, attr)
    await state.force(target)
    # A check that asserts on a session carries it as `.session`, so
    # the name of the check runs the session it checks.
    if not await target.has("spec") and await target.has("session"):
        target = await select_attr_path(state, target, ["session"])
    return target


# The session again with one more module on one node: its peers see the
# change, as they would had the module been there from the start. An
# unknown node is refused first: `nodes.<name>` would declare a new guest.
EXTENDED: Final = """session: node: module:
  let
    names = builtins.attrNames session.nodes;
    extended = session.extend { modules = [ { nodes.${node}.imports = [ (/. + module) ]; } ]; };
    build = extended.nodes.${node}.system.build;
  in
  if !(builtins.elem node names) then
    throw "no guest ${node} in this session; it has: ${builtins.concatStringsSep ", " names}"
  else
    { system = build.toplevel; registration = build.vivariumNixRegistration; }
"""

Action = Literal["switch", "boot", "test", "dry-activate"]


async def build_extended(file: Path, attr: list[str], node: str, module: Path) -> tuple[str, str]:
    """The node's system with *module* added, and its closure's registration."""
    async with evaluator(Recorder(None, sys.stderr)) as (state, _):
        target = await session_of(state, file, attr)
        extend = await state.eval_expr(EXTENDED)
        extended = await (await (await extend(target))(await state.make_string(node)))(
            await state.make_string(str(module.resolve()))
        )
        system = await (await select_attr_path(state, extended, ["system"])).realise_string()
        registration = await (await select_attr_path(state, extended, ["registration"])).realise_string()
    return system, registration


def switch_code(node: str, system: str, registration: str, action: Action) -> str:
    """What the run executes: the closure into the guest, then the switch."""
    return (
        f"_guest = vms[{node!r}]\n"
        f"await _guest.add_closure({registration!r})\n"
        f"print((await _guest.switch_to({system!r}, {action!r}))[1], end='')\n"
    )


def add_switch(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    switching = sub.add_parser(
        "switch",
        help="add a module to one guest of a running session, build it on the host and switch to it",
    )
    switching.add_argument(
        "attr",
        nargs="?",
        help="the session the run was started from; by default the one `run.json` names",
    )
    switching.add_argument("--file", "-f", type=Path, help="with an attribute: as `run` takes it (default `.`)")
    switching.add_argument("--out", type=Path, required=True, help="the running session's --out")
    switching.add_argument("--node", required=True, help="the guest to change")
    switching.add_argument("--module", type=Path, required=True, help="a NixOS module file")
    switching.add_argument("--action", choices=["switch", "boot", "test", "dry-activate"], default="switch")


def add_reload(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    reloading = sub.add_parser(
        "reload",
        help=(
            "evaluate a paused session again and give the run its new phases; the guests stay."
            " Refused when a guest changed"
        ),
    )
    reloading.add_argument(
        "attr",
        nargs="?",
        help="the session the run was started from; by default the one `run.json` names",
    )
    reloading.add_argument("--file", "-f", type=Path, help="with an attribute: as `run` takes it (default `.`)")
    reloading.add_argument("--out", type=Path, required=True, help="the paused session's --out")


async def build_spec(file: Path, attr: list[str]) -> Path:
    """The session's spec, built as `run` builds it: `.driver` runs the
    phases' type check, and a reload of a phase that does not type check
    fails here, not in the run."""
    async with evaluator(Recorder(None, sys.stderr)) as (state, _):
        target = await session_of(state, file, attr)
        await (await select_attr_path(state, target, ["driver"])).realise_string()
        return Path(await (await select_attr_path(state, target, ["spec"])).realise_string())


def reload(args: argparse.Namespace) -> int:
    try:
        origin = origin_of(args.attr, args.file, args.out)
        attr = split_attr(origin.attr)
    except ValueError as error:
        say(str(error))
        return 2
    started = time.monotonic()
    say(f"evaluating {origin.attr}")
    try:
        spec = anyio.run(build_spec, origin.file, attr)
    except (NixError, AttrPathError) as error:
        say(f"evaluation failed: {explain(str(error))}")
        return 1
    say(f"built {spec} in {time.monotonic() - started:.1f}s")
    reply = anyio.run(control.request, args.out / control.SOCKET, control.Op.RELOAD, str(spec), (), None)
    if reply.result is not None:
        print(reply.result)
    if reply.error:
        print(reply.error.rstrip(), file=sys.stderr)
    return 0 if reply.ok else 1


def this_cli() -> Path:
    """This program, by the name a printed command uses: the venv's script
    beside its interpreter, not `sys.argv[0]`, which may be a relative name."""
    return Path(sys.executable).parent / "vivarium"


def origin_of(attr: str | None, file: Path | None, out: Path) -> control.Origin:
    """What to evaluate again: the attribute given, or the origin the run
    in `out` wrote to `run.json`. Raises ValueError when neither names one."""
    if attr is not None:
        return control.Origin(this_cli(), attr, (file or Path(".")).absolute())
    if file is not None:
        raise ValueError("--file needs an attribute")
    try:
        about = control.RunFile.from_json((out / control.RUN_FILE).read_text())
    except FileNotFoundError:
        raise ValueError(f"no run in {out}: name the attribute") from None
    if about.origin is None:
        raise ValueError(f"the run in {out} was not started by `vivarium run`: name the attribute")
    return about.origin


def switch(args: argparse.Namespace) -> int:
    try:
        origin = origin_of(args.attr, args.file, args.out)
        attr = split_attr(origin.attr)
    except ValueError as error:
        say(str(error))
        return 2
    started = time.monotonic()
    say(f"evaluating {origin.attr} with {args.module} on {args.node}")
    try:
        system, registration = anyio.run(build_extended, origin.file, attr, args.node, args.module)
    except (NixError, AttrPathError) as error:
        say(f"evaluation failed: {explain(str(error))}")
        return 1
    say(f"built {system} in {time.monotonic() - started:.1f}s")
    code = switch_code(args.node, system, registration, args.action)
    # Every argument given: zuban's TypeVarTuple does not count a defaulted parameter.
    reply = anyio.run(control.request, args.out / control.SOCKET, control.Op.EXEC, code, (), None)
    if reply.output:
        print(reply.output, end="" if reply.output.endswith("\n") else "\n")
    if reply.error:
        print(reply.error.rstrip(), file=sys.stderr)
    return 0 if reply.ok else 1


ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def explain(error: str) -> str:
    """A Nix error with its point first.

    Nix prints the stack from the outside in, so the line that says what
    is wrong comes last, under a trace of `writeTextFile` and `//`. A
    reader -- a person, or an agent reading a channel event that shows
    the last lines -- wants it first. Colour codes go too: this text
    lands in files and events, not only on a terminal.
    """
    plain = ANSI.sub("", error).rstrip()
    lines = plain.splitlines()
    last = max((i for i, line in enumerate(lines) if line.lstrip().startswith("error:")), default=None)
    if last is None:
        return plain
    point = "\n".join(lines[last:]).strip()
    trace = "\n".join(lines[:last]).rstrip()
    return f"{point}\n\n{trace}" if trace else point


def say(text: str) -> None:
    # Before a session exists, so there is no `emit` to go through yet.
    print(f"[vivarium] {text}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    head, tail = split_dashdash(argv)
    top = parser()
    args, rest = top.parse_known_args(head)
    if args.command not in EVALUATE:
        if rest:
            top.error(f"unrecognized arguments: {' '.join(rest)}")
        # `vivarium ctl --out o pytest ./tests -- -k x`, as the runner takes it.
        args.pytest_args = tail[1:]
        if args.command == "ctl":
            raise SystemExit(anyio.run(runner_cli.ctl, args))
        if args.command == "monitor":
            raise SystemExit(runner_cli.watch(args))
        if args.command == "reload":
            raise SystemExit(reload(args))
        if args.command == "down":
            raise SystemExit(anyio.run(down, args.out.absolute()))
        if args.command == "ls":
            raise SystemExit(anyio.run(listing, args.json))
        raise SystemExit(switch(args))
    request = parse(argv)
    if request.until_pause:
        raise SystemExit(until_pause(argv, request))
    origin = control.Origin(this_cli(), ".".join(request.attr), request.file.absolute())
    log = None if request.out is None or request.command != "run" else request.out / BUILD_LOG
    if log is not None and request.out is not None:
        announce(request.out, origin)
    recorder = Recorder(log, sys.stderr)
    started = time.monotonic()
    say(f"evaluating {'.'.join(request.attr)} from {entry(request.file)}")
    try:
        program = anyio.run(resolve, request.file, request.attr, request.command, recorder)
    except (NixError, AttrPathError) as error:
        explained = explain(str(error))
        recorder.failed(explained)
        say(f"evaluation failed: {explained}")
        raise SystemExit(1) from None
    finally:
        recorder.close()
    say(f"evaluated and built in {time.monotonic() - started:.1f}s")
    # Exec'd, not called: the program is the one the evaluated library
    # built, with its own `vivarium`. Calling this package's `vivarium` instead ran
    # a spec from a newer lib.nix with an older runner, and a phase failed
    # on an import the newer field existed to make work.
    if request.command == "run":
        # So the run prints a `switch` line: only this CLI can evaluate it again.
        os.environ[control.ORIGIN] = origin.to_json()
    os.execv(program, [program, *request.rest])


LOG: Final = "run.log"
"""Where `--until-pause` puts the run's own output."""


def until_pause(argv: list[str], request: Request) -> int:
    """`run --until-pause`: the same run in its own session, then wait for it.

    The child is this command without the flag, so it evaluates, builds and
    becomes the runner as any `vivarium run` does. It outlives this
    process: the point is to return at a pause with the guests up."""
    if request.out is None:
        say(f"{'up' if request.command == 'up' else '--until-pause'} needs --out")
        return 2
    request.out.mkdir(parents=True, exist_ok=True)
    child_argv = [str(this_cli()), *child_args(argv)]
    with (request.out / LOG).open("wb") as log:
        child = subprocess.Popen(  # noqa: S603 -- this program, with the caller's own arguments
            child_argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
    # Its pid now, before the child writes it: a child that dies at once
    # is then a run gone, not a run never seen.
    origin = control.Origin(this_cli(), ".".join(request.attr), request.file.absolute())
    about = control.run_file(this_cli(), request.out, child.pid, control=False, origin=origin)
    (request.out / control.RUN_FILE).write_text(about.to_json())
    say(f"started the run, pid {child.pid}, output in {request.out / LOG}")
    args = argparse.Namespace(
        target=str(request.out), json=False, quiet=True, through_pauses=False, wait=monitor.WAIT
    )
    if request.command != "up":
        return runner_cli.watch(args)
    # stdout is `up`'s result, access.json; the progress is not.
    with contextlib.redirect_stdout(sys.stderr):
        code = runner_cli.watch(args)
    if code != monitor.READY:
        say(f"the environment is not ready (monitor exit {code}); `vivarium ctl --out {request.out} state` says why")
        return code
    print((request.out / access.FILE).read_text(), end="")
    say(f"ready: ssh -F {request.out / access.SSH_CONFIG} <guest>; files in {request.out / access.DIRECTORY}")
    say(f"stop it: {this_cli()} down --out {request.out}")
    return 0


def child_args(argv: list[str]) -> list[str]:
    """The `run` that `run --until-pause` or `up` starts in the background:
    `up` is `run --hold`, with the flag before any `--`."""
    head, tail = split_dashdash([arg for arg in argv if arg != "--until-pause"])
    if head and head[0] == "up":
        head = ["run", *head[1:], "--hold"]
    return [*head, *tail]


async def down(out: Path) -> int:
    """`ctl stop`, then the run's exit: 0 if its phases passed, 5 if it was
    stopped before it was ready, 1 if a phase failed."""
    socket = out / control.SOCKET
    try:
        reply = await control.request(socket, control.Op.STOP)
    except OSError as error:
        say(f"no run is listening at {socket}: {error}")
        return 1
    if not reply.ok:
        say(reply.error or "the run refused stop")
        return 1
    if not await runner_cli.wait_for_exit(out):
        say(f"the run did not exit within {runner_cli.STOP_WAIT:g}s of stop")
        return 1
    try:
        verdict = json.loads(await anyio.Path(out / "phases.json").read_text())
    except (OSError, json.JSONDecodeError) as error:
        say(f"the run left no verdict in {out / 'phases.json'}: {error}")
        return 2
    if verdict.get("stopped"):
        say(f"down; stopped before it was ready ({verdict['stopped']})")
        return STOPPED
    say("down" if verdict.get("passed") else "down; a phase failed")
    return 0 if verdict.get("passed") else 1


async def listing(as_json: bool) -> int:
    for up in await registry.runs():
        if as_json:
            print(json.dumps({"out": str(up.out), "state": up.state, "name": up.name, "pause": up.pause}))
        else:
            print("\t".join([up.state, up.name or "-", str(up.out)]))
    return 0


def announce(out: Path, origin: control.Origin) -> None:
    """`run.json` before the evaluation: the pid, which the exec keeps, and
    the monitor line. A monitor armed now follows the build in
    `build.jsonl`, and sees the run gone if it dies first. The runner
    writes the whole file once it has a socket."""
    out.mkdir(parents=True, exist_ok=True)
    about = control.run_file(this_cli(), out, os.getpid(), control=False, origin=origin)
    (out / control.RUN_FILE).write_text(about.to_json())


if __name__ == "__main__":
    main()
