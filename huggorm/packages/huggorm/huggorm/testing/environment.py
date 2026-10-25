"""Hermetic Nix stores for a test suite: a chroot store, or one behind its own daemon.

Every store here is a chroot store: `root=` moves the physical store under a
temporary directory and leaves the logical store dir at `/nix/store`. Nix
builds in such a store on Linux alone (`derivation-builder.cc` throws
"building using a diverted store is not supported on this platform"
elsewhere), so a test that builds carries `LINUX_CHROOT_BUILD`.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import os
import shutil
import signal
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from huggorm_bindings import is_experimental_feature

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

BACKENDS = ("local", "daemon")

#: The features every store and daemon here runs with. The daemon gets them on
#: its command line, because otherwise it reads the host's `nix.conf` and
#: disagrees with the client on any host that enables a different set. Only
#: those this Nix still calls experimental: Determinate Nix made `flakes`
#: stable, and refuses to enable it.
EXPERIMENTAL_FEATURES = tuple(
    feature
    for feature in (
        "ca-derivations",
        "dynamic-derivations",
        "fetch-tree",
        "flakes",
        "nix-command",
        "recursive-nix",
    )
    if is_experimental_feature(feature)
)

#: Puts the fixtures on the store of the machine, not a chroot store. A host
#: that cannot build in a chroot store (macOS) builds there. The machine's
#: store is shared, so a test that writes it leaves a path behind.
SYSTEM_STORE_ENV = "NANOPYNIX_TEST_SYSTEM_STORE"

LINUX_CHROOT_BUILD = pytest.mark.nix_platform("linux")
"""The test builds a derivation in a chroot store, which only Linux can do."""

# A Nix worker can finish releasing a store entry just after it says it shut
# down, so `ENOTEMPTY` on removal is a race. Twenty tries at 0.05 s is longer
# than any release measured.
_RMTREE_ATTEMPTS = 20
_PR_SET_PDEATHSIG = 1


def use_system_store() -> bool:
    return os.environ.get(SYSTEM_STORE_ENV, "") not in ("", "0")


@dataclass(frozen=True)
class NixTestEnvironment:
    """One hermetic store endpoint."""

    backend: str
    root: Path
    store_uri: str
    #: False for the store of the machine, which `root` does not hold.
    relocated: bool = True

    def pynix_store_args(self) -> list[str]:
        return ["--store", self.store_uri]

    def physical_path(self, store_path: str) -> Path:
        """Where a logical `/nix/store/...` path is on disk."""
        if not self.relocated:
            return Path(store_path)
        return self.root / store_path.removeprefix("/")

    def store_uri_matches(self, uri: str) -> bool:
        """Whether *uri*, as an open store reports it, names this environment's store.

        Nix may report a daemon store as `daemon`, and older versions drop
        the `root` parameter, so a daemon store matches on its scheme.
        """
        if uri == self.store_uri:
            return True
        if self.backend != "daemon":
            return False
        return uri == "daemon" or uri.startswith(("daemon?", "unix://"))


def with_nixpkgs(source: str, nixpkgs_path: str) -> str:
    """Put a hermetic path in place of a literal `<nixpkgs>`."""
    return source.replace("<nixpkgs>", nixpkgs_path)


def force_rmtree(path: Path) -> None:
    """Remove a closed store root, though Nix made entries read-only."""

    def onexc(function: Callable[..., object], raw_path: str, _exc: BaseException) -> None:
        failed = Path(raw_path)
        for fix in (failed, failed.parent):
            with contextlib.suppress(FileNotFoundError):
                fix.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        function(raw_path)

    for attempt in range(_RMTREE_ATTEMPTS):
        if not path.exists():
            return
        try:
            shutil.rmtree(path, onexc=onexc)
        except OSError as error:
            if error.errno != errno.ENOTEMPTY or attempt == _RMTREE_ATTEMPTS - 1:
                raise
            time.sleep(0.05)
        else:
            return


def _die_with_parent() -> None:  # pyright: ignore[reportUnusedFunction] -- named below on Linux only
    """SIGKILL this process when pytest dies, teardown or not."""
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)


# macOS has no PR_SET_PDEATHSIG, so a pytest killed without teardown leaves
# its daemon running there.
_PDEATHSIG_PREEXEC = _die_with_parent if sys.platform == "linux" else None


@dataclass
class _Daemon:
    process: subprocess.Popen[bytes]
    socket_path: Path

    def close(self) -> None:
        if self.process.poll() is not None:
            return
        # The daemon forks a worker per connection into its own process group.
        os.killpg(self.process.pid, signal.SIGTERM)
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(self.process.pid, signal.SIGKILL)
            self.process.wait()


def _start_daemon(root: Path) -> _Daemon:
    socket_path = root / "nix" / "var" / "nix" / "daemon-socket" / "socket"
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    log_path = root / "nix-daemon.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            [
                "nix",
                "daemon",
                "--store",
                f"local://{root}",
                "--option",
                "build-users-group",
                "",
                "--option",
                "require-drop-supplementary-groups",
                "false",
                # A daemon run as a user cannot use cgroups, and a host
                # `nix.conf` that turns them on stops it at startup.
                "--option",
                "use-cgroups",
                "false",
                "--option",
                "experimental-features",
                " ".join(EXPERIMENTAL_FEATURES),
            ],
            env={**os.environ, "NIX_DAEMON_SOCKET_PATH": str(socket_path)},
            start_new_session=True,
            preexec_fn=_PDEATHSIG_PREEXEC,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    # A poll, bounded: the daemon sends nothing when its socket is up.
    for _ in range(100):
        if socket_path.is_socket():
            break
        if process.poll() is not None:
            raise RuntimeError(
                f"temporary nix daemon exited with status {process.returncode}; "
                f"log:\n{log_path.read_text(errors='replace')}"
            )
        time.sleep(0.05)
    else:
        process.terminate()
        process.wait()
        raise RuntimeError(f"temporary nix daemon did not create {socket_path}")
    # The daemon lays out the store on its first real operation, and a client
    # that reads store files directly sees ENOENT before that.
    warmup = subprocess.run(
        ["nix", "store", "info", "--store", f"unix://{socket_path}?root={root}"],
        check=False,
        capture_output=True,
    )
    if warmup.returncode != 0 or not (root / "nix" / "store").is_dir():
        raise RuntimeError(
            f"temporary nix daemon warmup exited {warmup.returncode} without creating "
            f"{root / 'nix' / 'store'}; log:\n{log_path.read_text(errors='replace')}"
        )
    return _Daemon(process, socket_path)


@contextlib.contextmanager
def nix_environment(
    backend: str, root: Path, *, allow_system_store: bool = True
) -> Iterator[NixTestEnvironment]:
    """A store endpoint under *root*, removed with *root* on exit.

    *allow_system_store* is False for a store a test may collect garbage in:
    pointed at the machine's store, that would delete the machine's paths.
    """
    daemon: _Daemon | None = None
    try:
        if allow_system_store and use_system_store():
            uri = {"local": "local://", "daemon": "daemon"}[backend]
            yield NixTestEnvironment(backend=backend, root=root, store_uri=uri, relocated=False)
        elif backend == "local":
            # `local://`, not `local`: Nix reports an open store with the `//`.
            yield NixTestEnvironment(backend=backend, root=root, store_uri=f"local://?root={root}")
        elif backend == "daemon":
            daemon = _start_daemon(root)
            yield NixTestEnvironment(
                backend=backend, root=root, store_uri=f"unix://{daemon.socket_path}?root={root}"
            )
        else:
            raise ValueError(f"unknown Nix test backend: {backend!r}")
    finally:
        if daemon is not None:
            daemon.close()
        force_rmtree(root)


def configured_backends(config: pytest.Config) -> tuple[str, ...]:
    raw = config.getoption("--nix-test-backends")
    if not isinstance(raw, str):
        raise pytest.UsageError("--nix-test-backends must be a comma-separated string")
    values = tuple(value.strip() for value in raw.split(",") if value.strip())
    if not values or set(values) - set(BACKENDS):
        raise pytest.UsageError(f"--nix-test-backends accepts {','.join(BACKENDS)}; got {raw!r}")
    return values


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--nix-test-backends",
        default="local",
        help="comma-separated hermetic Nix backends: local,daemon",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "nix_platform(name): run only on the named `sys.platform`")


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "nix_backend" in metafunc.fixturenames:
        metafunc.parametrize("nix_backend", configured_backends(metafunc.config), scope="session")


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        marker = item.get_closest_marker("nix_platform")
        if marker is None:
            continue
        if marker.kwargs or len(marker.args) != 1 or not isinstance(marker.args[0], str):
            raise pytest.UsageError("nix_platform requires one platform name")
        if sys.platform != marker.args[0]:
            item.add_marker(
                pytest.mark.skip(reason=f"needs {marker.args[0]}, and this host is {sys.platform}")
            )


@pytest.fixture(scope="session")
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture(scope="session")
def shared_nix_environment(
    nix_backend: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[NixTestEnvironment]:
    """One store per backend, shared by every test that asks for it."""
    with nix_environment(
        nix_backend, tmp_path_factory.mktemp(f"nix-{nix_backend}-shared")
    ) as environment:
        yield environment


@pytest.fixture
def isolated_nix_environment(
    nix_backend: str, tmp_path_factory: pytest.TempPathFactory
) -> Iterator[NixTestEnvironment]:
    """A store of this test's own, always a chroot store.

    Not under `tmp_path`: a test often evaluates that directory as a path
    flake, and Nix refuses the daemon socket in it.
    """
    root = tmp_path_factory.mktemp(f"nix-{nix_backend}")
    with nix_environment(nix_backend, root, allow_system_store=False) as environment:
        yield environment
