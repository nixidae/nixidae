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
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, NamedTuple

import anyio
import huggorm
from huggorm.attrpath import AttrPathError, auto_call, select_attr_path
from huggorm.errors import NixError
from huggorm_bindings import DerivedPathBuilt, OutputsSpec
from vivarium import access, cli as runner_cli, control, environments, liveness, logs, monitor, places, registry
from vivarium.exits import Exit, describe
from vivarium.phases import VERDICT, Verdict
from vivarium_runner.forward import InboundJson

from . import daemon, shell
from .build_log import FILE as BUILD_LOG, SHOWN, Recorder

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Sequence

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
    first `run.json` go there. `run` and `up` take it; `phases` does not."""
    until_pause: bool = False
    daemonize: bool = False


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

RUNS: Final = frozenset({"run", "up"})
"""The commands that take the runner's `run` options."""

TAKES_NO_VALUE: Final = frozenset({"store_true", "store_false", "count"})


class Forward(argparse.Action):
    """Keeps a runner option as the caller wrote it, in `forward`. The exec'd
    runner reads the words again, and a parsed value such as `--cpus`'s does
    not turn back into them."""

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,
        values: str | Sequence[Any] | None,  # Any: what argparse hands an action
        option_string: str | None = None,
    ) -> None:
        words = [] if values is None else [values] if isinstance(values, str) else [str(value) for value in values]
        namespace.forward = [*namespace.forward, option_string, *words]


def forwarding(words: argparse.ArgumentParser) -> Callable[..., argparse.Action]:
    """An `add_argument` for `add_run_options` that records each option's
    words in `forward` and checks nothing: `parser` checks them. The caller
    gives `words` the default `forward=[]`."""

    def add(*flags: str, **options: Any) -> argparse.Action:  # Any: add_argument's own keywords
        nargs = 0 if options.get("action") in TAKES_NO_VALUE else None
        return words.add_argument(*flags, action=Forward, nargs=nargs, dest="forward")

    return add


def parser() -> argparse.ArgumentParser:
    """Every command. `ctl` and `monitor` come from the runner's parser,
    so the two programs take them the same way."""
    top = argparse.ArgumentParser(
        prog="vivarium",
        description="Evaluate a run, build it and run it; reach into a run; follow it",
        epilog=(
            "run and up take the runner's options; run, up and phases hand an option they do not know"
            " to the runner: `vivarium run x --out o -v -- -k name`."
        ),
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
            default=Path(),
            help="the file or directory to evaluate, as `nix build --file` takes it",
        )
        if command == "up":
            runner_cli.add_run_options(optional_out(evaluate.add_argument))
            evaluate.add_argument(
                "--name",
                help=(
                    "the environment's name, when no --out is given: its directory is"
                    " <state>/envs/NAME. The default is the test's `name`"
                ),
            )
            evaluate.add_argument(
                "--daemonize",
                action="store_true",
                help=(
                    "run it as a transient unit of the user's systemd, vivarium-NAME.service, so it"
                    " outlives the caller's cgroup; it ends at logout unless the user lingers."
                    " Without a user systemd, the run detaches as without the flag"
                ),
            )
        elif command in RUNS:
            runner_cli.add_run_options(evaluate.add_argument)
        if command == "run":
            evaluate.add_argument(
                "--until-pause",
                action="store_true",
                help=(
                    "start the run in the background, its output in <out>/run.log; wait as"
                    f" `monitor --quiet` does and exit with its status ({describe()})."
                    " The run goes on after a pause"
                ),
            )
    stopping = sub.add_parser("down", help="stop an environment `up` started; exit 0 if its phases passed")
    add_target(stopping)
    stopping.set_defaults(handler=lambda args: anyio.run(down, target_of(args)))
    for command in ("ps", "ls"):
        lister = sub.add_parser(command, help="the runs up on this host: their state, environment, test and --out")
        lister.add_argument("--json", action="store_true", help="one JSON object per run")
        lister.set_defaults(handler=lambda args: anyio.run(listing, args.json))
    removing = sub.add_parser("rm", help="delete stopped environments' directories")
    removing.add_argument("names", nargs="+", metavar="NAME")
    removing.add_argument("--force", "-f", action="store_true", help="stop one that is up first")
    removing.set_defaults(handler=lambda args: anyio.run(remove, args.names, args.force))
    cleaning = sub.add_parser("clean", help="delete every environment that is not up, and stale run entries")
    cleaning.add_argument("--dry-run", action="store_true", help="say what would go, delete nothing")
    cleaning.set_defaults(handler=lambda args: anyio.run(clean, args.dry_run))
    for command, what in (
        ("env", "a live run's variables as `export` lines, for a shell's `eval`"),
        ("shell", "the user's shell, or the command after --, with a live run's variables"),
    ):
        entering = sub.add_parser(command, help=what)
        add_target(entering)
        entering.add_argument(
            "--no-proxy",
            dest="proxied",
            action="store_false",
            help="leave out the lab's SOCKS5 proxy: no ALL_PROXY, HTTPS_PROXY or unproxy",
        )
        if command == "env":
            entering.add_argument("--json", action="store_true", help="one JSON object")
        entering.set_defaults(handler=enter)
    loading = sub.add_parser(
        "load",
        help=(
            "import image tarballs (`docker save`, OCI) into a live cluster's nodes, as `kind load`:"
            " `load NAME IMAGE...` or `load --out DIR IMAGE...`"
        ),
    )
    loading.add_argument("words", nargs="+", metavar="[NAME] IMAGE", help="the environment, then the tarballs")
    loading.add_argument("--out", type=Path, help="the run's --out, for a run that is no named environment")
    loading.add_argument("--node", action="append", default=[], help="this node only; repeatable. Default: every node")
    loading.set_defaults(handler=load)
    noding = sub.add_parser("node", help="add a worker to a live cluster from a template, or remove one added so")
    noding.set_defaults(handler=node)
    actions = noding.add_subparsers(dest="action", required=True)
    adding = actions.add_parser("add", help="`node add NAME NODE` or `node add --out DIR NODE`: launch and join NODE")
    adding.add_argument("--template", help="the template to launch; default: the run's one Kubernetes template")
    removing = actions.add_parser("rm", help="`node rm NAME NODE` or `node rm --out DIR NODE`: delete and stop NODE")
    for action in (adding, removing):
        action.add_argument("words", nargs="+", metavar="[NAME] NODE", help="the environment, then the node")
        action.add_argument("--out", type=Path, help="the run's --out, for a run that is no named environment")
    removing.set_defaults(template=None)
    closing = sub.add_parser(
        "closure", help="every store path these reach, one a line: what a test in a guest needs of the host's store"
    )
    closing.add_argument("paths", nargs="+", help="store paths")
    closing.set_defaults(handler=lambda args: anyio.run(closure, args.paths))
    add_switch(sub)
    add_reload(sub)
    runner_cli.add_ctl(sub)
    runner_cli.add_monitor(sub)
    logs.add(sub)
    return top


def optional_out(add: Callable[..., argparse.Action]) -> Callable[..., argparse.Action]:
    """`add` with `--out` optional: `up` names an environment instead."""

    def added(*names: str, **options: Any) -> argparse.Action:  # Any: argparse's own keyword arguments
        if "--out" in names:
            options["required"] = False
        return add(*names, **options)

    return added


def add_target(command: argparse.ArgumentParser) -> None:
    """An environment by name, or a run by its --out."""
    command.add_argument("name", nargs="?", help="the environment's name, as `vivarium ps` lists it")
    command.add_argument("--out", type=Path, help="the run's --out, for a run that is no named environment")


def target_of(args: argparse.Namespace) -> Path:
    if (args.name is None) == (args.out is None):
        raise SystemExit(f"vivarium {args.command}: name an environment or give --out, one of them")
    try:
        return places.environment(args.name) if args.out is None else args.out.absolute()
    except places.PlaceError as error:
        raise SystemExit(f"vivarium {args.command}: {error}") from None


def split_dashdash(argv: list[str]) -> tuple[list[str], list[str]]:
    """Before `--`, and from it on. argparse drops `--`, and the runner
    needs it to know where pytest's arguments begin."""
    if "--" not in argv:
        return argv, []
    at = argv.index("--")
    return argv[:at], argv[at:]


def parse(argv: list[str]) -> Request:
    """`run`, `up` or `phases`: our options, and the runner's words as the
    caller wrote them. The runner's options are checked here, before an
    evaluation; one this copy of them does not know goes on unchecked, for
    the runner the evaluation builds."""
    head, tail = split_dashdash(argv)
    top = parser()
    args, unknown = top.parse_known_args(head)
    if args.command not in EVALUATE:
        top.error(f"{args.command} does not evaluate")
    try:
        attr = split_attr(args.attr)
    except ValueError as error:
        top.error(str(error))
    words = argparse.ArgumentParser(add_help=False)
    words.add_argument("command")
    words.add_argument("attr")
    words.add_argument("--file", "-f")
    words.add_argument("--until-pause", action="store_true")
    words.set_defaults(forward=[])
    if args.command in RUNS:
        runner_cli.add_run_options(forwarding(words))
    return Request(
        args.command,
        args.file,
        attr,
        [*words.parse_known_args(head)[0].forward, *unknown, *tail],
        args.out if args.command in RUNS else None,
        until_pause=args.command == "up" or (args.command == "run" and args.until_pause),
        daemonize=args.command == "up" and args.daemonize,
    )


async def resolve(file: Path, attr: list[str], command: str, recorder: Recorder) -> str:
    """Evaluate, build the attribute's `.driver` or `.phases`, and return
    the program in it.

    That program, and not this package's `vivarium`, is what runs: it carries
    the `vivarium` of the library that was evaluated, which knows every field
    of the spec. Building `.driver` also runs the type check of every phase
    script, exactly as `nix run --file . <attr>.driver` does.
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
    {
      system = build.toplevel;
      registration = build.vivariumNixRegistration;
      # The spec's drvPath forces the run's checks, a fixed host port
      # asked for twice among them, as a fresh run would.
      inbound = builtins.seq extended.spec.drvPath (builtins.toJSON extended.nodes.${node}.vivarium.inbound);
    }
"""


async def build_extended(file: Path, attr: list[str], node: str, module: Path) -> tuple[str, str, list[InboundJson]]:
    """The node's system with *module* added, its closure's registration,
    and its `vivarium.inbound`."""
    async with evaluator(Recorder(None, sys.stderr)) as (state, _):
        target = await session_of(state, file, attr)
        extend = await state.eval_expr(EXTENDED)
        extended = await (await (await extend(target))(await state.make_string(node)))(
            await state.make_string(str(module.resolve()))
        )
        system = await (await select_attr_path(state, extended, ["system"])).realise_string()
        registration = await (await select_attr_path(state, extended, ["registration"])).realise_string()
        inbound: list[InboundJson] = json.loads(
            await (await select_attr_path(state, extended, ["inbound"])).realise_string()
        )
    return system, registration, inbound


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
    switching.add_argument("--action", choices=control.SWITCH_ACTIONS, default="switch")
    switching.set_defaults(handler=switch)


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
    reloading.set_defaults(handler=reload)


async def build_spec(file: Path, attr: list[str]) -> Path:
    """The session's spec, built as `run` builds it: `.driver` runs the
    phases' type check, and a reload of a phase that does not type check
    fails here, not in the run."""
    async with evaluator(Recorder(None, sys.stderr)) as (state, _):
        target = await session_of(state, file, attr)
        await (await select_attr_path(state, target, ["driver"])).realise_string()
        return Path(await (await select_attr_path(state, target, ["spec"])).realise_string())


def reload(args: argparse.Namespace) -> Exit:
    try:
        origin = origin_of(args.attr, args.file, args.out)
        attr = split_attr(origin.attr)
    except ValueError as error:
        say(str(error))
        return Exit.USAGE
    started = time.monotonic()
    say(f"evaluating {origin.attr}")
    try:
        spec = anyio.run(build_spec, origin.file, attr)
    except (NixError, AttrPathError) as error:
        say(f"evaluation failed: {explain(str(error))}")
        return Exit.FAILED
    say(f"built {spec} in {time.monotonic() - started:.1f}s")
    reply = anyio.run(control.request, places.socket(args.out, control.SOCKET), control.Op.RELOAD, str(spec), (), None)
    if reply.result is not None:
        print(reply.result)
    if reply.error:
        print(reply.error.rstrip(), file=sys.stderr)
    return reply.status


def this_cli() -> Path:
    """This program, by the name a printed command uses: the venv's script
    beside its interpreter, not `sys.argv[0]`, which may be a relative name."""
    return Path(sys.executable).parent / "vivarium"


def origin_of(attr: str | None, file: Path | None, out: Path) -> control.Origin:
    """What to evaluate again: the attribute given, or the origin the run
    in `out` wrote to `run.json`. Raises ValueError when neither names one."""
    if attr is not None:
        return control.Origin(this_cli(), attr, (file or Path()).absolute())
    if file is not None:
        raise ValueError("--file needs an attribute")
    try:
        about = control.RunFile.from_json((out / control.RUN_FILE).read_text())
    except FileNotFoundError:
        raise ValueError(f"no run in {out}: name the attribute") from None
    if about.origin is None:
        raise ValueError(f"the run in {out} was not started by `vivarium run`: name the attribute")
    return about.origin


def switch(args: argparse.Namespace) -> Exit:
    try:
        origin = origin_of(args.attr, args.file, args.out)
        attr = split_attr(origin.attr)
    except ValueError as error:
        say(str(error))
        return Exit.USAGE
    started = time.monotonic()
    say(f"evaluating {origin.attr} with {args.module} on {args.node}")
    try:
        system, registration, inbound = anyio.run(build_extended, origin.file, attr, args.node, args.module)
    except (NixError, AttrPathError) as error:
        say(f"evaluation failed: {explain(str(error))}")
        return Exit.FAILED
    say(f"built {system} in {time.monotonic() - started:.1f}s")
    asked = control.SwitchRequest(args.node, system, registration, args.action, inbound).encode()
    # Every argument given: zuban's TypeVarTuple does not count a defaulted parameter.
    reply = anyio.run(control.request, places.socket(args.out, control.SOCKET), control.Op.SWITCH, asked, (), None)
    if reply.output:
        print(reply.output, end="" if reply.output.endswith("\n") else "\n")
    if reply.error:
        print(reply.error.rstrip(), file=sys.stderr)
    return reply.status


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
    # A runner this execs may be older and read only its own variables.
    places.pin()
    head, tail = split_dashdash(argv)
    top = parser()
    args, rest = top.parse_known_args(head)
    if args.command not in EVALUATE:
        if rest:
            top.error(f"unrecognized arguments: {' '.join(rest)}")
        # `vivarium ctl --out o pytest ./tests -- -k x`, as the runner takes it.
        args.pytest_args = tail[1:]
        raise SystemExit(args.handler(args))
    if head and head[0] == "up":
        argv = name_environment(argv)
    request = parse(argv)
    if request.until_pause:
        raise SystemExit(until_pause(argv, request))
    if request.command == "run" and request.out is not None:
        # From before the evaluation, which takes minutes: the run is live
        # from here, and the runner this execs keeps the same lock.
        try:
            liveness.hand_down(liveness.hold(request.out))
        except liveness.RunLive as error:
            say(str(error))
            raise SystemExit(Exit.NO_RUN) from None
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
        raise SystemExit(Exit.NO_VERDICT) from None
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
        raise RuntimeError(f"{request.command}: argparse lets no run start without --out")
    request.out.mkdir(parents=True, exist_ok=True)
    # `up NAME` checked already; `up --out` and `run --until-pause` did not.
    if anyio.run(environments.live, request.out):
        say(f"a run is live in {request.out}; `vivarium down --out {request.out}` first")
        return Exit.NO_RUN
    forget_last_run(request.out)
    child_argv = [str(this_cli()), *child_args(argv)]
    systemd_run = daemon.manager() if request.daemonize else None
    if request.daemonize and systemd_run is None:
        say("no user systemd answers: the run detaches in a session of its own, as without --daemonize")
    if systemd_run is not None:
        try:
            unit, pid = daemon.start(systemd_run, request.out, request.out / LOG, Path.cwd(), os.environ, child_argv)
        except daemon.DaemonError as error:
            say(str(error))
            return Exit.NO_RUN
        how = f"as {unit}"
    else:
        with (request.out / LOG).open("wb") as log:
            child = subprocess.Popen(  # noqa: S603 -- this program, with the caller's own arguments
                child_argv, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
        # Reaped as it ends: an unreaped child stays a zombie, and the monitor's
        # `kill(pid, 0)` then finds the run alive until --wait runs out
        # (nixidae/nixidae#1353). Daemon: the run outlives this process.
        threading.Thread(target=child.wait, name="reap-run", daemon=True).start()
        pid, how = child.pid, "in a session of its own"
    # Its pid now, before the child writes it: a child that dies at once
    # is then a run gone, not a run never seen.
    origin = control.Origin(this_cli(), ".".join(request.attr), request.file.absolute())
    about = control.run_file(this_cli(), request.out, pid, control=False, origin=origin)
    (request.out / control.RUN_FILE).write_text(about.to_json())
    say(f"started the run {how}, pid {pid}, output in {request.out / LOG}")
    args = argparse.Namespace(target=str(request.out), json=False, quiet=True, through_pauses=False, wait=monitor.WAIT)
    if request.command != "up":
        return runner_cli.watch(args)
    # stdout is `up`'s result, access.json; the progress is not.
    with contextlib.redirect_stdout(sys.stderr):
        code = runner_cli.watch(args)
    if code != Exit.READY:
        say(not_ready(code, request.out))
        return code
    print((request.out / access.FILE).read_text(), end="")
    say(f"ready: ssh -F {request.out / access.SSH_CONFIG} <guest>; files in {request.out / access.DIRECTORY}")
    say(f"stop it: {this_cli()} down --out {request.out}")
    return Exit.PASSED


def forget_last_run(out: Path) -> None:
    """Remove what the last run in *out* left for the monitor.

    The monitor takes `events.jsonl` for a run that is up, and the new run
    writes its own only after it evaluates and builds. Without this, a second
    `up` of an environment reads the last run's ready and verdict, and ends
    while its run still boots."""
    for name in (logs.EVENTS, VERDICT):
        (out / name).unlink(missing_ok=True)
    liveness.forget(out)


def not_ready(code: int, out: Path) -> str:
    """Where to look when `up` ends short of ready. Only a paused run
    still has its socket to answer `ctl state`; an ended one left files."""
    if code == Exit.PAUSED:
        return f"the environment is not ready, it paused; `vivarium ctl --out {out} state` says why"
    return f"the environment is not ready (monitor exit {code}); see {out / LOG} and {out / VERDICT}"


def child_args(argv: list[str]) -> list[str]:
    """The `run` that `run --until-pause` or `up` starts in the background:
    `up` is `run --hold`, with the flag before any `--`."""
    head, tail = split_dashdash([arg for arg in argv if arg not in ("--until-pause", "--daemonize")])
    if head and head[0] == "up":
        head = ["run", *head[1:], "--hold"]
    return [*head, *tail]


async def closure(paths: list[str]) -> int:
    """`closure`: what `nix-store --query --requisites` prints, from the store."""
    huggorm.load_config()
    async with huggorm.AsyncSession() as session:
        store = session.store()
        roots = [await store.parse_store_path(path) for path in paths]
        for path in await store.compute_fs_closure(roots):
            print(await store.print_store_path(path))
    return 0


async def down(out: Path) -> Exit:
    """`ctl stop`, then the run's verdict: passed, failed, or stopped before
    it was ready."""
    socket = places.socket(out, control.SOCKET)
    try:
        reply = await control.request(socket, control.Op.STOP)
    except OSError as error:
        say(f"no run is listening at {socket}: {error}")
        return Exit.NO_RUN
    if not reply.ok:
        say(reply.error or "the run refused stop")
        return reply.status
    if not await runner_cli.wait_for_exit(out):
        say(f"the run did not exit within {runner_cli.STOP_WAIT:g}s of stop")
        return Exit.FAILED
    try:
        verdict = Verdict.from_json(await anyio.Path(out / VERDICT).read_text())
    except (OSError, ValueError) as error:
        say(f"the run left no verdict in {out / VERDICT}: {error}")
        return Exit.NO_VERDICT
    if verdict.stopped is not None:
        say(f"down; stopped before it was ready ({verdict.stopped})")
        return Exit.STOPPED
    say("down" if verdict.passed else "down; a phase failed")
    return Exit.PASSED if verdict.passed else Exit.FAILED


async def listing(as_json: bool) -> int:
    """Every live run, then every environment that is down, with its state `down`."""
    live = await registry.runs()
    rows = [(up.state, up.environment, up.name, up.out, up.pause) for up in live]
    seen = {up.out for up in live}
    rows += [
        ("down", one.name, None, one.out, None) for one in await environments.every() if one.out.resolve() not in seen
    ]
    for state, environment, test, out, pause in rows:
        if as_json:
            print(
                json.dumps({"out": str(out), "state": state, "environment": environment, "name": test, "pause": pause})
            )
        else:
            print("\t".join([state, environment or "-", test or "-", str(out)]))
    return 0


def name_environment(argv: list[str]) -> list[str]:
    """`up` with no --out: `--out <state>/envs/<name>` in place of `--name`.
    The name is `--name`, else the test's `name`, which this evaluates."""
    head, tail = split_dashdash(argv)
    # The whole parser, so a runner option before the attribute keeps its
    # value: `up --only boot checks.k8s`.
    known, _ = parser().parse_known_args(head)
    if known.out is not None:
        if known.name is not None:
            raise SystemExit("vivarium up: --name and --out name the same thing; give one")
        return argv
    try:
        name = known.name or anyio.run(test_name, known.file, split_attr(known.attr))
    except (NixError, AttrPathError) as error:
        say(f"evaluation failed: {explain(str(error))}")
        raise SystemExit(Exit.NO_VERDICT) from None
    try:
        out = places.environment(name)
    except places.PlaceError as error:
        raise SystemExit(f"vivarium up: {error}; pick one with --name") from None
    if anyio.run(environments.live, out):
        raise SystemExit(f"vivarium up: the environment {name} is up; `vivarium down {name}` first, or pick --name")
    say(f"environment {name}: {out}")
    return [*without_name(head), "--out", str(out), *tail]


def without_name(words: list[str]) -> list[str]:
    """*words* in their order, less `--name NAME` and `--name=NAME`."""
    kept: list[str] = []
    skip = False
    for word in words:
        if skip:
            skip = False
        elif word == "--name":
            skip = True
        elif not word.startswith("--name="):
            kept.append(word)
    return kept


def enter(args: argparse.Namespace) -> int:
    """`env` and `shell`: a live run only, since a stopped one's variables
    name ports another run may hold now. The run hands them over."""
    out = target_of(args)
    if not anyio.run(environments.live, out):
        print(f"vivarium {args.command}: {out} is not up; `vivarium up` it first", file=sys.stderr)
        return Exit.NO_RUN
    try:
        found = shell.gather(out, anyio.run(shell.exported, out), proxied=args.proxied)
        if args.command == "shell":
            shell.enter(found, args.pytest_args)
    except (shell.ShellError, places.PlaceError) as error:
        print(f"vivarium {args.command}: {error}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(found, indent=2, sort_keys=True))
    else:
        print(shell.script(found), end="")
    return 0


LOADED: Final = "vivarium-load: "
"""The prefix of the one output line that carries `load`'s or `node`'s result."""


def live_run(command: str, words: list[str], out: Path | None) -> Path | Exit:
    """The run `command` acts on: *out*, else the environment the first of
    *words* names, which it takes; refused when it is not up."""
    try:
        found = out.absolute() if out is not None else places.environment(words.pop(0))
    except places.PlaceError as error:
        print(f"vivarium {command}: {error}", file=sys.stderr)
        return Exit.USAGE
    if not anyio.run(environments.live, found):
        print(f"vivarium {command}: {found} is not up; `vivarium up` it first", file=sys.stderr)
        return Exit.NO_RUN
    return found


def in_run(command: str, out: Path, code: str, function: str) -> Any | None:  # Any: the JSON the code printed
    """Run *code* in the live run through `exec`, which a run takes while
    its phases run as at a pause, and return the JSON it printed after
    `LOADED`; None, said on stderr, when it failed. *function* is the
    `vivarium_k8s` function it calls, for the error of an older run."""
    reply = anyio.run(control.request, places.socket(out, control.SOCKET), control.Op.EXEC, code, (), None)
    found = [line.removeprefix(LOADED) for line in reply.output.splitlines() if line.startswith(LOADED)]
    if reply.ok and found:
        return json.loads(found[-1])
    said = reply.error or reply.output
    if function in said and ("has no attribute" in said or "cannot import" in said):
        said = f"this run's vivarium_k8s has no {function}: bring it up again with this vivarium"
    elif "No module named 'vivarium_k8s'" in said:
        said = "this run has no Kubernetes nodes (vivarium.recipes.kubernetes)"
    print(f"vivarium {command}: {said.rstrip()}", file=sys.stderr)
    return None


def load(args: argparse.Namespace) -> int:
    """`load`: `vivarium_k8s.load_images` in the live run. stdout is the
    images each node has, as JSON."""
    words: list[str] = list(args.words)
    if len(words) < (1 if args.out is not None else 2):
        print("vivarium load: name the image tarballs", file=sys.stderr)
        return Exit.USAGE
    out = live_run("load", words, args.out)
    if isinstance(out, Exit):
        return out
    tarballs = [str(Path(word).absolute()) for word in words]
    if missing := [tarball for tarball in tarballs if not Path(tarball).is_file()]:
        print(f"vivarium load: no file {missing}", file=sys.stderr)
        return Exit.USAGE
    request = json.dumps([tarballs, args.node])
    # Run with the run's own vivarium_k8s, which this CLI does not carry.
    code = (
        "import json as _json\n"
        "import vivarium_k8s as _k8s\n"
        "from vivarium_k8s.images import latest as _latest\n"
        f"_loaded = await _k8s.load_images(vms, *_json.loads({request!r}))\n"
        "_refs = sorted({ref for found in _loaded.values() for ref in found})\n"
        f"print({LOADED!r} + _json.dumps({{'loaded': _loaded, 'latest': [r for r in _refs if _latest(r)]}}))\n"
    )
    result = in_run("load", out, code, "load_images")
    if result is None:
        return Exit.FAILED
    for ref in result["latest"]:
        say(f"{ref} is `latest`: a pod needs imagePullPolicy IfNotPresent or Never to run it, or kubelet pulls")
    print(json.dumps(result["loaded"], indent=2, sort_keys=True))
    return Exit.PASSED


def node(args: argparse.Namespace) -> int:
    """`node add` and `node rm`: `vivarium_k8s.add_node` or `remove_node`
    in the live run. stdout is the node, as JSON."""
    words: list[str] = list(args.words)
    command = f"node {args.action}"
    if len(words) != (1 if args.out is not None else 2):
        print(f"vivarium {command}: name the environment, unless --out, then one node", file=sys.stderr)
        return Exit.USAGE
    out = live_run(command, words, args.out)
    if isinstance(out, Exit):
        return out
    request = json.dumps([words[0], args.template])
    if args.action == "add":
        function = "add_node"
        call = (
            f"_name, _template = _json.loads({request!r})\n"
            "_found = sorted(_k8s.templates(vms))\n"
            "if _template is None and len(_found) != 1:\n"
            "    raise ValueError(f'name a template with --template; this run has {_found}')\n"
            "_vm = await _k8s.add_node(vms, _template or _found[0], _name)\n"
            f"print({LOADED!r} + _json.dumps({{'name': _vm.name, 'ip': _vm.ip}}))\n"
        )
    else:
        function = "remove_node"
        call = (
            f"_name, _ = _json.loads({request!r})\n"
            "await _k8s.remove_node(vms, _name)\n"
            f"print({LOADED!r} + _json.dumps({{'removed': _name}}))\n"
        )
    code = "import json as _json\nimport vivarium_k8s as _k8s\n" + call
    result = in_run(command, out, code, function)
    if result is None:
        return Exit.FAILED
    print(json.dumps(result, indent=2, sort_keys=True))
    return Exit.PASSED


async def test_name(file: Path, attr: list[str]) -> str:
    """The `name` the test's module configuration gives."""
    async with evaluator(Recorder(None, sys.stderr)) as (state, _):
        target = await session_of(state, file, attr)
        return await (await select_attr_path(state, target, ["config", "name"])).realise_string()


async def remove(names: list[str], force: bool) -> int:
    """`rm`: each named environment's directory; one that is up only with --force."""
    failed = False
    for name in names:
        try:
            out = places.environment(name)
        except places.PlaceError as error:
            say(str(error))
            failed = True
            continue
        if not out.is_dir():
            say(f"no environment {name}")
            failed = True
            continue
        if await environments.live(out):
            if not force:
                say(f"{name} is up; `vivarium down {name}` first, or rm --force")
                failed = True
                continue
            await down(out)
            # A run that is still evaluating has no socket to stop it by.
            if await environments.live(out):
                say(f"{name} is still up after `down`; its disks stay")
                failed = True
                continue
        try:
            await anyio.to_thread.run_sync(environments.remove, out)
        except (OSError, environments.Refused, subprocess.CalledProcessError) as error:
            say(f"{name}: {error}")
            failed = True
            continue
        say(f"removed {name}")
    return 1 if failed else 0


async def clean(dry_run: bool) -> int:
    """`clean`: every environment that is down. `registry.runs` drops dead
    run entries on the way."""
    await registry.runs()
    failed = False
    for one in await environments.every():
        if one.up:
            continue
        if dry_run:
            print(one.name)
            continue
        try:
            await anyio.to_thread.run_sync(environments.remove, one.out)
        except (OSError, environments.Refused, subprocess.CalledProcessError) as error:
            say(f"{one.name}: {error}")
            failed = True
            continue
        say(f"removed {one.name}")
    return 1 if failed else 0


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
