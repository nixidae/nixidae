"""`pytest` drives vivarium: guests as fixtures. nixidae/nixidae#1413.

The guests are a `mkTest` in a Nix file, named from Python:

    # pytest.ini
    [pytest]
    vivarium_file = tests/machines.nix

    @pytest.mark.vivarium("pair", scope="class")
    class TestPair:
        async def test_reach(self, vms):
            await vms["a"].succeed("ping -c1 b")

    @pytest.mark.vivarium("one", scope="function")
    async def test_fresh(vm):
        assert (await vm.succeed("hostname")).stdout == "vm"

    @pytest.mark.vivarium("boxes", template="box", dirty=True)
    async def test_shared(vm):
        await vm.succeed("true")

With `template=`, the test's guest comes from the run's pool
(`vivarium.pool`): a booted spare of that `mkTest` template, so the test
waits for no boot, terminated after its scope. `dirty=True` takes a
guest an earlier dirty scope gave back, and gives it back for the next.
Every pooled test of one `attr` shares one run, up for the whole
session. Under pytest-xdist the workers share it too: the first starts
it, the others take it, and the controller stops it at the end. So a
dirty guest one worker gives back serves the next dirty test of any.

**The run is its own process, not this one.** vivarium needs a user and
mount namespace; entered here, every test in the run would be root of
it. So the plugin is a client: it starts a run, the run's
`machines.sock` serves each guest's methods (`vivarium.remote`), and a
test gets proxies. The run stops when the last test of its scope is
done. `--vivarium-keep` leaves the runs up, and the next `pytest` takes
them as they are: no evaluation, no boot.

A run starts one of two ways. In a dev shell, `vivarium up` evaluates
`vivarium_file` through huggorm. Under `VIVARIUM_RUNS`, a JSON file of
`{attr: program}` that `pytestCheck` builds, each program is a built
`.up`, and nothing evaluates: that is how the same suite runs in a Nix
sandbox.

This module imports nothing of huggorm, so the sandbox's Python needs
none. A test marked `vivarium` that is `async def` runs on the plugin's
loop, where the connections live, and so does each of its async
fixtures (`vivarium.async_fixtures`), which may then use `vms`. A test
marked `anyio` too is refused: anyio's plugin runs it on a loop of its
own, where the connections do not work.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import functools
import inspect
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import anyio
import coverage
import pytest
from anyio.abc import SocketAttribute
from anyio.from_thread import start_blocking_portal
from vivarium_agent.arpyc import AsyncConnection, connect

from . import cli as runner_cli, control, monitor, places
from .async_fixtures import bound, in_loop, is_async
from .control import reachable
from .exits import Exit
from .remote import SOCKET, RemoteMachine

if TYPE_CHECKING:
    from collections.abc import Generator

    from anyio.from_thread import BlockingPortal

type Scope = Literal["function", "class", "module", "session"]

SCOPES: Final[tuple[Scope, ...]] = ("function", "class", "module", "session")
PREBUILT: Final = "VIVARIUM_RUNS"
"""A JSON file of `{attr: program}`, each a built `.up`: runs that need no evaluation."""
CLOSURE: Final = "VIVARIUM_PYTHON_CLOSURE"
"""A closureInfo of this Python, which `pytestCheck` builds: a test in a guest runs on it."""
LOG: Final = "run.log"
GUEST_ARTIFACTS: Final = "/artifacts"
GUEST_TIMEOUT: Final = 3600.0
"""What one test in a guest may take, its fixtures' setup and teardown included."""
WORKER_SOCKETS: Final = "/run/vivarium/pytest"
POOLS: Final = "vivarium_pools"
"""The `workerinput` key that names the pool runs' directory to each xdist worker."""


class RunFailed(RuntimeError):
    """A run that did not come up."""


@dataclass(frozen=True)
class Want:
    """What one test asks for: a run of `attr`, shared over `scope`, and
    with `guest` the guest the test itself runs in."""

    attr: str
    scope: Scope
    guest: str | None = None
    template: str | None = None
    """The pool template the test's guest comes from, if any."""
    dirty: bool = False

    @classmethod
    def of(cls, item: pytest.Item) -> Want | None:
        mark = item.get_closest_marker("vivarium")
        if mark is None:
            return None
        if len(mark.args) != 1 or not isinstance(mark.args[0], str):
            raise pytest.UsageError(f"{item.nodeid}: vivarium(attr) takes one attribute name")
        scope = mark.kwargs.get("scope", "class")
        if scope not in SCOPES:
            raise pytest.UsageError(f"{item.nodeid}: vivarium scope is one of {', '.join(SCOPES)}, not {scope!r}")
        guest = mark.kwargs.get("guest")
        if guest is not None and not isinstance(guest, str):
            raise pytest.UsageError(f"{item.nodeid}: vivarium guest is a guest's name, not {guest!r}")
        template = mark.kwargs.get("template")
        if template is not None and not isinstance(template, str):
            raise pytest.UsageError(f"{item.nodeid}: vivarium template is a template's name, not {template!r}")
        if template is not None and guest not in (None, template):
            raise pytest.UsageError(f"{item.nodeid}: with template={template!r}, the guest it runs in is {template!r}")
        dirty = mark.kwargs.get("dirty", False)
        if not isinstance(dirty, bool) or (dirty and template is None):
            raise pytest.UsageError(f"{item.nodeid}: vivarium dirty is True or False, and needs template=")
        return cls(mark.args[0], scope, guest, template, dirty)


def scope_key(item: pytest.Item, scope: Scope) -> str:
    """The node a run is shared under: the test, its class, its module, or
    the whole session. A test outside a class shares its module's."""
    if scope == "function":
        return item.nodeid
    if scope == "class" and (cls := item.getparent(pytest.Class)) is not None:
        return cls.nodeid
    if scope in ("class", "module") and (module := item.getparent(pytest.Module)) is not None:
        return module.nodeid
    return ""


def run_key(item: pytest.Item) -> tuple[str, str] | None:
    want = Want.of(item)
    return None if want is None else (want.attr, scope_key(item, want.scope))


def ends_after(item: pytest.Item, nextitem: pytest.Item | None) -> bool:
    """Whether `item`'s run has no use after it: pytest runs a scope's
    tests together, so a next test with another key never comes back."""
    key = run_key(item)
    return key is not None and (nextitem is None or run_key(nextitem) != key)


def out_name(attr: str) -> str:
    """A directory name for a run of `attr`."""
    return re.sub(r"[^A-Za-z0-9_.-]", "-", attr)


type BatchKey = tuple[str, str, str]
"""A run's key and the guest: the tests that run in one guest pytest."""


def batch_key(item: pytest.Item) -> BatchKey | None:
    want = Want.of(item)
    if want is None or want.guest is None:
        return None
    return (want.attr, scope_key(item, want.scope), want.guest)


def batches(items: list[pytest.Item]) -> dict[BatchKey, list[str]]:
    """The tests that run in a guest, grouped by run and guest, in
    pytest's order: each group is one pytest in that guest."""
    grouped: dict[BatchKey, list[str]] = {}
    for item in items:
        if (key := batch_key(item)) is not None:
            grouped.setdefault(key, []).append(item.nodeid)
    return grouped


def batch_files(rootdir: Path, nodeids: list[str]) -> list[str]:
    """The files that hold `nodeids`, each once, in order, as absolute
    paths: pytest reads a path from where it starts, and a node id is the
    rootdir's.

    Files, not node ids, go to the guest's pytest: a node id it cannot
    collect is a usage error that ends it, while a file that fails to
    import is a collection error, which the tests that need it report."""
    return list(dict.fromkeys(str(rootdir / nodeid.partition("::")[0]) for nodeid in nodeids))


def store_root(path: str) -> str:
    """The store path a file belongs to: `/nix/store/<hash>-<name>`."""
    parts = Path(path).parts
    if parts[:3] != ("/", "nix", "store") or len(parts) < 4:
        raise pytest.UsageError(f"a test in a guest needs a Python from the Nix store; this one is {path}")
    return str(Path(*parts[:4]))


SKIP: Final = frozenset({".vivarium", ".git", ".jj", "__pycache__", ".pytest_cache", ".pytest-agent"})
"""What the project's tarball leaves out: runs, history and caches."""


def project_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = Path(info.name).name
    if name in SKIP or name.startswith("result") or not (info.isfile() or info.isdir() or info.issym()):
        return None
    return info


@dataclass(frozen=True)
class Measure:
    """pytest-cov's measurement, which a test in a guest repeats there.

    Read from pytest-cov's options, not its objects. The guest's data
    file goes beside `data_file` as a parallel file, and pytest-cov's
    own combine at the end merges it with the host's."""

    data_file: Path
    sources: tuple[str, ...] | None
    branch: bool
    config: str

    @classmethod
    def of(cls, config: pytest.Config) -> Measure | None:
        if not config.pluginmanager.hasplugin("_cov") or config.getoption("no_cov", default=False):
            return None
        raw: list[str | bool] = config.getoption("cov_source", default=[]) or []
        # As pytest-cov reads it: a bare --cov measures everything.
        sources = None if True in raw else tuple(source for source in raw if isinstance(source, str))
        file = str(config.getoption("cov_config", default=".coveragerc"))
        data_file = Path(coverage.Coverage(config_file=file).config.data_file).absolute()
        return cls(data_file, sources, bool(config.getoption("cov_branch", default=False)), file)

    def worker_args(self, data_file: str) -> list[str]:
        args = ["--coverage", data_file, "--cov-config", self.config]
        if self.branch:
            args.append("--cov-branch")
        for source in self.sources or ():
            args += ["--cov-source", source]
        return args


@dataclass
class Run:
    attr: str
    out: Path
    conn: AsyncConnection
    vms: dict[str, RemoteMachine] = field(default_factory=dict)


@dataclass
class Box:
    """What one scope holds: its run, and the guests its tests see."""

    run: Run
    vms: dict[str, RemoteMachine]
    lease: str | None = None
    """The pool guest's name, for a test with `template=`."""
    dirty: bool = False


@dataclass
class GuestWorker:
    """The pytest that runs one batch's tests in a guest (`vivarium.pytest_worker`)."""

    box: Box
    guest: str
    stem: str
    host: Path
    """The host's side of the guest's `/artifacts/pytest`."""
    items: frozenset[str]
    collect_errors: list[pytest.CollectReport]

    @property
    def socket(self) -> str:
        return f"{WORKER_SOCKETS}/{self.stem}.sock"

    @property
    def log(self) -> Path:
        return self.host / f"{self.stem}.log"

    def said(self) -> str:
        """The worker's own output, for an error that has no other evidence."""
        return self.log.read_text(errors="replace") if self.log.exists() else f"(no {self.log})"


async def _connect(out: Path) -> AsyncConnection:
    with reachable(places.socket(out, SOCKET)) as name:
        stream = await anyio.connect_unix(name)
    # arpyc reads a raw fd from the loop; the duplicate outlives the stream. The caller serves it.
    fd = os.dup(stream.extra(SocketAttribute.raw_socket).fileno())
    await stream.aclose()
    return connect(fd)


async def _stop(out: Path) -> None:
    """`vivarium down`: `stop`, then wait for the run to exit."""
    reply = await control.request(places.socket(out, control.SOCKET), control.Op.STOP)
    if not reply.ok:
        raise RunFailed(f"the run at {out} refused stop: {reply.error}")
    if not await runner_cli.wait_for_exit(out):
        raise RunFailed(f"the run at {out} did not exit within {runner_cli.STOP_WAIT:g}s of stop")


class Driver:
    """The runs of one pytest session, and the loop their connections live on."""

    def __init__(self, config: pytest.Config) -> None:
        file = config.getini("vivarium_file")
        self.file: Path | None = config.rootpath / file if file else None
        self.base = Path(config.getoption("vivarium_out") or config.rootpath / ".vivarium" / "pytest")
        self.keep: bool = config.getoption("vivarium_keep")
        prebuilt = os.environ.get(PREBUILT)
        self.prebuilt: dict[str, str] | None = json.loads(Path(prebuilt).read_text()) if prebuilt else None
        self.spares: int = config.getoption("vivarium_spares")
        self.boxes: dict[tuple[str, str], Box] = {}
        self.pools: dict[str, Run] = {}
        """The run each pooled `attr` shares, up for the session."""
        # xdist sets `workerinput` on a worker's config only.
        shared = getattr(config, "workerinput", {}).get(POOLS)
        self.owns_pools = shared is None
        """Whether this process stops the pool runs: a lone pytest or the xdist controller."""
        self.pool_dir = Path(shared) if shared else self.base / ("pools" if self.keep else f"pools-{time.time_ns()}")
        self.rootdir = config.rootpath
        self.inifile = config.inipath
        # The guest's pytest starts where this one did, so a relative --cov
        # or --cov-config means the same there; the project holds only the rootdir.
        here = Path.cwd()
        self.cwd = here if here.is_relative_to(self.rootdir) else self.rootdir
        self.coverage = Measure.of(config)
        self.batches: dict[BatchKey, list[str]] = {}
        self.workers: dict[BatchKey, GuestWorker] = {}
        self.idle: dict[tuple[Path, str], GuestWorker] = {}
        """Workers kept in dirty guests after their batch, by run and guest: the next batch there reuses one."""
        self.prepared: set[tuple[Path, str]] = set()
        self._portal: BlockingPortal | None = None
        self._portal_cm: Any = None  # anyio's portal context manager, entered by hand
        self.current: pytest.Item | None = None
        """The test pytest runs now: whether its async fixtures are this plugin's."""

    @property
    def portal(self) -> BlockingPortal:
        if self._portal is None:
            self._portal_cm = start_blocking_portal()
            self._portal = self._portal_cm.__enter__()
        return self._portal

    def acquire(self, item: pytest.Item) -> Box:
        want = Want.of(item)
        if want is None:
            raise pytest.UsageError(f"{item.nodeid}: vms and vm need @pytest.mark.vivarium(attr)")
        key = (want.attr, scope_key(item, want.scope))
        if key in self.boxes:
            return self.boxes[key]
        if want.template is None:
            run = self._up(want.attr, out_name(want.attr))
            self.boxes[key] = Box(run, run.vms)
            return self.boxes[key]
        run = self.pools.get(want.attr) or self._pool_run(want.attr)
        self.pools[want.attr] = run
        name = str(self.portal.call(run.conn.call, "acquire", (want.template, not want.dirty, self.spares), {}))
        self.boxes[key] = Box(run, {want.template: RemoteMachine(run.conn, name)}, name, want.dirty)
        return self.boxes[key]

    def _up(self, attr: str, name: str) -> Run:
        out = self.base / name if self.keep else self.base / f"{name}-{time.time_ns()}"
        return self._take(attr, out, reuse=self.keep)

    def _take(self, attr: str, out: Path, *, reuse: bool) -> Run:
        if reuse and places.socket(out, SOCKET).exists():
            with contextlib.suppress(OSError):
                return self._open(attr, out)
        self._start(attr, out)
        return self._open(attr, out)

    def _pool_run(self, attr: str) -> Run:
        """The pool run of `attr` that every process of this session shares:
        started by the first to ask, under a lock the others wait on."""
        self.pool_dir.mkdir(parents=True, exist_ok=True)
        name = out_name(attr)
        with (self.pool_dir / f"{name}.lock").open("w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return self._take(attr, self.pool_dir / name, reuse=True)

    def _start(self, attr: str, out: Path) -> None:
        if self.prebuilt is None:
            self._evaluate_and_start(attr, out)
            return
        program = self.prebuilt.get(attr)
        if program is None:
            raise pytest.UsageError(
                f"{attr} is not among the runs {PREBUILT} names: {', '.join(sorted(self.prebuilt))}"
            )
        out.mkdir(parents=True, exist_ok=True)
        with (out / LOG).open("wb") as log:
            subprocess.Popen(  # noqa: S603 -- a built `.up` the check names
                [program, "--out", str(out)],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        args = argparse.Namespace(target=str(out), json=False, quiet=True, through_pauses=False, wait=monitor.WAIT)
        # The monitor prints the pause; it is progress, not a test's output.
        with contextlib.redirect_stdout(sys.stderr):
            code = runner_cli.watch(args)
        if code != Exit.READY:
            raise RunFailed(f"{attr} did not come up (exit {code}); its output is in {out / LOG}")

    def _evaluate_and_start(self, attr: str, out: Path) -> None:
        if self.file is None:
            raise pytest.UsageError("set vivarium_file in the pytest configuration")
        program = shutil.which("vivarium")
        if program is None:
            raise pytest.UsageError("starting a run from vivarium_file needs the `vivarium` CLI on PATH")
        # Its stdout is access.json, which `--out` holds; the progress is on stderr.
        subprocess.run(  # noqa: S603 -- the vivarium CLI
            [program, "up", attr, "--file", str(self.file), "--out", str(out)], check=True, stdout=subprocess.DEVNULL
        )

    def _open(self, attr: str, out: Path) -> Run:
        conn = self.portal.call(_connect, out)
        # In the portal's task group, which outlives each test that calls on it.
        self.portal.start_task(conn.serve_forever)
        names: tuple[str, ...] = self.portal.call(conn.call, "machines", (), {})
        return Run(attr, out, conn, {name: RemoteMachine(conn, name) for name in names})

    # ── tests that run in a guest ───────────────────────────────────

    def _python_closure(self) -> list[str]:
        """This Python's store closure: from `pytestCheck`'s closureInfo, or
        from the `vivarium` CLI, which asks the store."""
        if info := os.environ.get(CLOSURE):
            return [line for line in (Path(info) / "store-paths").read_text().splitlines() if line]
        program = shutil.which("vivarium")
        if program is None:
            raise pytest.UsageError("a test in a guest needs the `vivarium` CLI on PATH, or " + CLOSURE)
        done = subprocess.run(  # noqa: S603 -- the vivarium CLI
            [program, "closure", store_root(sys.prefix)], check=True, capture_output=True, text=True
        )
        return done.stdout.split()

    def _prepare(self, box: Box, guest: str) -> Path:
        """This Python in the guest's store view, and the project unpacked
        at its own path: once a guest. Returns the host's side of the
        guest's `/artifacts`."""
        vm = box.vms[guest]
        artifacts = self.portal.call(vm.artifacts)
        if artifacts is None:
            raise RunFailed(f"{guest} has no /artifacts to hand it the project through")
        host = Path(artifacts) / "pytest"
        if (box.run.out, vm.name) in self.prepared:
            return host
        self.portal.call(vm.add_paths, self._python_closure())
        host.mkdir(parents=True, exist_ok=True)
        with tarfile.open(host / "project.tar", "w") as tar:
            tar.add(self.rootdir, arcname=".", filter=project_filter)
        unpack = "import sys, tarfile; tarfile.open(sys.argv[1]).extractall(sys.argv[2], filter='tar')"
        command = shlex.join([sys.executable, "-c", unpack, f"{GUEST_ARTIFACTS}/pytest/project.tar", str(self.rootdir)])
        self.portal.call(functools.partial(vm.succeed, f"mkdir -p {shlex.quote(str(self.rootdir))} && {command}"))
        self.prepared.add((box.run.out, vm.name))
        return host

    def _start_worker(self, item: pytest.Item, key: BatchKey) -> GuestWorker:
        """A pytest in the guest that has collected the batch's tests and
        waits for the host to name each one: one kept in the guest, or a new one."""
        box = self.acquire(item)
        guest = key[2]
        files = batch_files(self.rootdir, self.batches.get(key, [item.nodeid]))
        kept = self.idle.pop((box.run.out, box.vms[guest].name), None)
        if kept is not None:
            kept.box = box
            return self._collect(item, key, kept, files)
        host = self._prepare(box, guest)
        stem = f"worker-{time.time_ns()}"
        worker = [
            sys.executable,
            "-m",
            "vivarium.pytest_worker",
            f"{WORKER_SOCKETS}/{stem}.sock",
            f"{GUEST_ARTIFACTS}/pytest/{stem}.log",
        ]
        if self.coverage is not None:
            worker += self.coverage.worker_args(f"{GUEST_ARTIFACTS}/pytest/{stem}.coverage")
        args = ["--rootdir", str(self.rootdir), "-q"]
        if self.inifile is not None:
            args += ["-c", str(self.inifile)]
        command = shlex.join([*worker, "--", *args, *files])
        vm = box.vms[guest]
        # Returns once the worker listens and has left the command.
        start = f"cd {shlex.quote(str(self.cwd))} && PYTHONDONTWRITEBYTECODE=1 {command} 2>&1"
        self.portal.call(functools.partial(vm.succeed, start))
        return self._collect(item, key, GuestWorker(box, guest, stem, host, frozenset(), []), None)

    def _collect(self, item: pytest.Item, key: BatchKey, worker: GuestWorker, files: list[str] | None) -> GuestWorker:
        """What `worker` collected: at its start, or now from *files*."""
        try:
            collected = json.loads(str(self._ask(worker, "collect", *([] if files is None else [files]))))
        except Exception as error:
            raise RunFailed(f"pytest in {worker.guest} did not collect: {error}\n{worker.said()}") from error
        worker.items = frozenset(collected["items"])
        worker.collect_errors = [
            item.config.hook.pytest_report_from_serializable(config=item.config, data=data)
            for data in collected["reports"]
        ]
        self.workers[key] = worker
        return worker

    def _ask(self, worker: GuestWorker, method: str, *args: object) -> object:
        vm = worker.box.vms[worker.guest]
        return self.portal.call(functools.partial(vm.relay, worker.socket, method, args, timeout=GUEST_TIMEOUT))

    def _end_batch(self, key: BatchKey) -> None:
        """After a batch's last test, which tore down every fixture: a dirty
        guest keeps its worker for the next batch there, any other ends it."""
        worker = self.workers.get(key)
        if worker is not None and worker.box.dirty:
            del self.workers[key]
            self.idle[(worker.box.run.out, worker.box.vms[worker.guest].name)] = worker
            return
        self._stop_worker(key)

    def _stop_worker(self, key: BatchKey) -> None:
        worker = self.workers.pop(key, None)
        if worker is not None:
            self._stop(worker)

    def _stop(self, worker: GuestWorker) -> None:
        """End the worker's session: pytest tears down what is still set
        up, and coverage.py saves."""
        try:
            self._ask(worker, "stop")
        except Exception as error:
            raise RunFailed(f"pytest in {worker.guest} did not stop: {error}\n{worker.said()}") from error
        if self.coverage is not None and (measured := worker.host / f"{worker.stem}.coverage").exists():
            # Beside pytest-cov's own data file, as a parallel file: its combine merges it.
            shutil.copyfile(measured, f"{self.coverage.data_file}.vivarium-{worker.guest}-{worker.stem}")

    def guest_reports(self, item: pytest.Item, nextitem: pytest.Item | None) -> list[pytest.TestReport]:
        """Run `item` in its batch's guest worker, and return its reports.
        The worker stops after the last test of its batch."""
        key = batch_key(item)
        if key is None:
            raise RuntimeError(f"{item.nodeid} does not run in a guest")
        worker = self.workers.get(key) or self._start_worker(item, key)
        nextid = nextitem.nodeid if nextitem is not None and batch_key(nextitem) == key else None
        try:
            if item.nodeid not in worker.items:
                errors = "\n".join(report.longreprtext for report in worker.collect_errors)
                return [_failed(item, f"its guest's pytest did not collect it\n{errors}".rstrip())]
            try:
                found = json.loads(str(self._ask(worker, "run", item.nodeid, nextid)))
            except Exception as error:
                # Gone, or no longer answering: nothing to stop.
                self.workers.pop(key, None)
                raise RunFailed(f"pytest in {worker.guest} failed to run it: {error}\n{worker.said()}") from error
            return [
                item.config.hook.pytest_report_from_serializable(config=item.config, data=data)
                for data in found
                if data.get("$report_type") == "TestReport"
            ]
        finally:
            if nextid is None:
                self._end_batch(key)

    def release(self, key: tuple[str, str]) -> None:
        box = self.boxes.pop(key, None)
        if box is None:
            return
        # Those `-x` left running: their sessions end before the guest does.
        for batch in [batch for batch, worker in self.workers.items() if worker.box is box]:
            self._stop_worker(batch)
        if box.lease is not None:
            if not box.dirty:
                self.prepared.discard((box.run.out, box.lease))
            self.portal.call(box.run.conn.call, "release", (box.lease, box.dirty), {})
            return
        self._down(box.run)

    def _down(self, run: Run, *, stop: bool = True) -> None:
        self.prepared = {done for done in self.prepared if done[0] != run.out}
        # On its loop: closing wakes a write that waits on the fd there.
        self.portal.call(run.conn.close, None)
        if stop and not self.keep:
            self.portal.call(_stop, run.out)

    def close(self) -> None:
        for key in list(self.boxes):
            self.release(key)
        # Before the runs stop: each saves its coverage on the way out.
        for worker in self.idle.values():
            self._stop(worker)
        self.idle.clear()
        for run in self.pools.values():
            self._down(run, stop=False)
        self.pools.clear()
        if self.owns_pools and not self.keep and self.pool_dir.is_dir():
            # Every worker has closed: the owner ends the runs they shared.
            for out in sorted(path for path in self.pool_dir.iterdir() if path.is_dir()):
                if places.socket(out, control.SOCKET).exists():
                    self.portal.call(_stop, out)
        if self._portal_cm is not None:
            self._portal_cm.__exit__(None, None, None)
            self._portal = self._portal_cm = None


_DRIVER = pytest.StashKey[Driver]()


def _driver(config: pytest.Config) -> Driver:
    driver = config.stash.get(_DRIVER, None)
    if driver is None:
        raise pytest.UsageError("the vivarium plugin is not configured")
    return driver


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini("vivarium_file", "the Nix file whose mkTest attributes @pytest.mark.vivarium names", default="")
    group = parser.getgroup("vivarium")
    group.addoption("--vivarium-keep", action="store_true", help="leave each run up, and take a run left up")
    group.addoption("--vivarium-out", default=None, help="where the runs' --out directories go")
    group.addoption(
        "--vivarium-spares",
        type=int,
        default=1,
        help="booted guests of each pool template kept ready for the next test",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "vivarium(attr, scope='class', guest=None, template=None, dirty=False): guests from the mkTest "
        "`attr` of vivarium_file, shared over function, class, module or session; with template=, one "
        "guest of that template from the run's pool",
    )
    config.stash[_DRIVER] = Driver(config)
    if config.pluginmanager.hasplugin("xdist"):
        config.pluginmanager.register(_XdistHooks(), "vivarium-xdist")


class _XdistHooks:
    """Registered only beside pytest-xdist: pluggy refuses a hook it does not know."""

    @staticmethod
    def pytest_configure_node(node: Any) -> None:  # noqa: ANN401 -- xdist's WorkerController, untyped
        node.workerinput[POOLS] = str(_driver(node.config).pool_dir)


def pytest_unconfigure(config: pytest.Config) -> None:
    driver = config.stash.get(_DRIVER, None)
    if driver is not None:
        driver.close()


@pytest.fixture
def vms(request: pytest.FixtureRequest) -> dict[str, RemoteMachine]:
    """Every guest of the test's run, by name."""
    return _driver(request.config).acquire(request.node).vms


@pytest.fixture
def vm(vms: dict[str, RemoteMachine]) -> RemoteMachine:
    """The guest of a run that has one."""
    if len(vms) != 1:
        raise pytest.UsageError(f"vm needs a run with one guest; this one has {', '.join(sorted(vms))}")
    return next(iter(vms.values()))


def _ours(item: pytest.Item) -> bool:
    """Whether the plugin's loop runs *item*'s async test and fixtures."""
    if Want.of(item) is None:
        return False
    if item.get_closest_marker("anyio") is not None:
        raise pytest.UsageError(
            f"{item.nodeid}: a test marked vivarium runs on the plugin's loop, where its guests are; drop the anyio mark"
        )
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None]:
    _driver(item.config).current = item
    yield


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None]:
    # `request.getfixturevalue` sets a fixture up during the call.
    _driver(item.config).current = item
    yield


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef[Any], request: pytest.FixtureRequest) -> Generator[None]:
    func = fixturedef.func
    item = _driver(request.config).current
    if not is_async(func) or item is None or not _ours(item):
        yield
        return
    fixturedef.func = in_loop(_driver(request.config).portal, bound(func, request))  # type: ignore[misc]  # pytest declares it Final; anyio's plugin swaps it too
    try:
        yield
    finally:
        # The definition is shared by every test that asks for it, so it goes back.
        fixturedef.func = func  # type: ignore[misc]  # Final in pytest, as above


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    test = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test) or not _ours(pyfuncitem):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    _driver(pyfuncitem.config).portal.call(functools.partial(test, **kwargs))
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None]:
    yield
    if ends_after(item, nextitem) and (key := run_key(item)) is not None:
        _driver(item.config).release(key)


def pytest_collection_finish(session: pytest.Session) -> None:
    # After `-k` and `--lf` chose: a guest runs only what was selected.
    _driver(session.config).batches = batches(session.items)


def _failed(item: pytest.Item, text: str) -> pytest.TestReport:
    return pytest.TestReport(item.nodeid, item.location, {}, "failed", text, "call")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item: pytest.Item, nextitem: pytest.Item | None) -> bool | None:
    """A test with `guest=` runs its whole protocol in that guest; here,
    only its reports are told, so every reporter sees an ordinary test."""
    if batch_key(item) is None:
        return None
    driver = _driver(item.config)
    item.ihook.pytest_runtest_logstart(nodeid=item.nodeid, location=item.location)
    try:
        reports = driver.guest_reports(item, nextitem) or [_failed(item, "its guest's pytest reported nothing for it")]
    except Exception as error:  # noqa: BLE001 -- the guest's failure, rebuilt by vinegar across machines.sock, is this test's
        reports = [_failed(item, f"vivarium could not run it in its guest: {error}")]
    for report in reports:
        item.ihook.pytest_runtest_logreport(report=report)
    item.ihook.pytest_runtest_logfinish(nodeid=item.nodeid, location=item.location)
    if ends_after(item, nextitem) and (key := run_key(item)) is not None:
        driver.release(key)
    return True
