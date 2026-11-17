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
        assert await vm.succeed("hostname") == "vm\\n"

**The run is its own process, not this one.** vivarium needs a user and
mount namespace; entered here, every test in the run would be root of
it. So the plugin is a client: `vivarium up` starts the run, its
`machines.sock` serves each guest's methods, and a test gets proxies.
`vivarium down` ends the run when the last test of its scope is done.
`--vivarium-keep` leaves the runs up, and the next `pytest` takes them
as they are: no evaluation, no boot.

A test that uses `vms` or `vm` is `async def` and runs on the plugin's
loop, where the connections live.
"""

from __future__ import annotations

import functools
import inspect
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import anyio
import pytest
from anyio.abc import SocketAttribute
from anyio.from_thread import start_blocking_portal
from vivarium.control import reachable
from vivarium.remote import SOCKET
from vivarium_runner.arpyc import AsyncConnection, connect

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Generator

    from anyio.from_thread import BlockingPortal

type Scope = Literal["function", "class", "module", "session"]

SCOPES: Final[tuple[Scope, ...]] = ("function", "class", "module", "session")
FIXTURES: Final = frozenset({"vms", "vm"})
DRIVER: Final = "vivarium-driver"


class RemoteMachine:
    """A guest of another process's run: every method of `Machine` as a
    coroutine, one round trip each. Arguments and results are plain data."""

    def __init__(self, conn: AsyncConnection, name: str) -> None:
        self._conn = conn
        self.name = name

    async def get(self, attribute: str) -> Any:  # noqa: ANN401 -- any attribute of a Machine
        """A plain attribute or property, such as `uplink_ip`."""
        return await self._conn.call("get", (self.name, attribute), {})

    def __getattr__(self, method: str) -> Callable[..., Coroutine[Any, Any, Any]]:
        if method.startswith("_"):
            raise AttributeError(method)

        async def call(*args: object, **kwargs: object) -> Any:  # noqa: ANN401 -- what the method returns
            return await self._conn.call("call", (self.name, method, args, kwargs), {})

        call.__name__ = method
        return call

    def __repr__(self) -> str:
        return f"<RemoteMachine {self.name}>"


@dataclass(frozen=True)
class Want:
    """What one test asks for: a run of `attr`, shared over `scope`."""

    attr: str
    scope: Scope

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
        return cls(mark.args[0], scope)


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


@dataclass
class Run:
    attr: str
    out: Path
    conn: AsyncConnection
    vms: dict[str, RemoteMachine] = field(default_factory=dict)


async def _connect(out: Path) -> AsyncConnection:
    with reachable(out / SOCKET) as name:
        stream = await anyio.connect_unix(name)
    # arpyc reads a raw fd from the loop; the duplicate outlives the stream.
    fd = os.dup(stream.extra(SocketAttribute.raw_socket).fileno())
    await stream.aclose()
    return connect(fd)


class Driver:
    """The runs of one pytest session, and the loop their connections live on."""

    def __init__(self, config: pytest.Config) -> None:
        file = config.getini("vivarium_file")
        self.file: Path | None = config.rootpath / file if file else None
        self.base = Path(config.getoption("vivarium_out") or config.rootpath / ".vivarium" / "pytest")
        self.keep: bool = config.getoption("vivarium_keep")
        self.runs: dict[tuple[str, str], Run] = {}
        self._portal: BlockingPortal | None = None
        self._portal_cm: Any = None  # anyio's portal context manager, entered by hand

    @property
    def portal(self) -> BlockingPortal:
        if self._portal is None:
            self._portal_cm = start_blocking_portal()
            self._portal = self._portal_cm.__enter__()
        return self._portal

    def _vivarium(self, *args: str | os.PathLike[str]) -> None:
        program = shutil.which("vivarium")
        if program is None:
            raise pytest.UsageError("the vivarium plugin needs the `vivarium` CLI on PATH")
        subprocess.run([program, *args], check=True)

    def acquire(self, item: pytest.Item) -> Run:
        want = Want.of(item)
        if want is None:
            raise pytest.UsageError(f"{item.nodeid}: vms and vm need @pytest.mark.vivarium(attr)")
        key = (want.attr, scope_key(item, want.scope))
        if key in self.runs:
            return self.runs[key]
        if self.file is None:
            raise pytest.UsageError("set vivarium_file in the pytest configuration")
        out = self.base / out_name(want.attr) if self.keep else self.base / f"{out_name(want.attr)}-{time.time_ns()}"
        if not (self.keep and (out / SOCKET).exists() and self._try_connect(out, key)):
            self._vivarium("up", want.attr, "--file", self.file, "--out", out)
            self._open(out, key)
        return self.runs[key]

    def _try_connect(self, out: Path, key: tuple[str, str]) -> bool:
        try:
            self._open(out, key)
        except OSError:
            return False
        return True

    def _open(self, out: Path, key: tuple[str, str]) -> None:
        conn = self.portal.call(_connect, out)
        names: tuple[str, ...] = self.portal.call(conn.call, "machines", (), {})
        self.runs[key] = Run(key[0], out, conn, {name: RemoteMachine(conn, name) for name in names})

    def release(self, key: tuple[str, str]) -> None:
        run = self.runs.pop(key, None)
        if run is None:
            return
        run.conn.close()
        if not self.keep:
            self._vivarium("down", "--out", run.out)

    def close(self) -> None:
        for key in list(self.runs):
            self.release(key)
        if self._portal_cm is not None:
            self._portal_cm.__exit__(None, None, None)
            self._portal = self._portal_cm = None


def _driver(config: pytest.Config) -> Driver:
    driver = config.stash.get(_DRIVER, None)
    if driver is None:
        raise pytest.UsageError("the vivarium plugin is not configured")
    return driver


_DRIVER = pytest.StashKey[Driver]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addini("vivarium_file", "the Nix file whose mkTest attributes @pytest.mark.vivarium names", default="")
    group = parser.getgroup("vivarium")
    group.addoption("--vivarium-keep", action="store_true", help="leave each run up, and take a run left up")
    group.addoption("--vivarium-out", default=None, help="where the runs' --out directories go")


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "vivarium(attr, scope='class'): guests from the mkTest `attr` of vivarium_file, "
        "shared over function, class, module or session",
    )
    config.stash[_DRIVER] = Driver(config)


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


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem: pytest.Function) -> bool | None:
    test = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test) or not FIXTURES & set(pyfuncitem.fixturenames):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    _driver(pyfuncitem.config).portal.call(functools.partial(test, **kwargs))
    return True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item: pytest.Item, nextitem: pytest.Item | None) -> Generator[None]:
    yield
    if ends_after(item, nextitem) and (key := run_key(item)) is not None:
        _driver(item.config).release(key)
