"""Host plumbing more than one backend uses: sockets, memory, helper processes, the router's egress."""

from __future__ import annotations

import functools
import os
import socket
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final

import anyio

from .. import container, forward, unixpath
from ..lifetime import die_with_parent
from .base import BackendError

if TYPE_CHECKING:
    from ..machine import Machine


def log_tail(path: Path, lines: int = 20) -> str:
    """The end of *path*, for a helper that has already exited.

    The end and not the start: a helper says why it gave up last.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError as error:
        return f"({path} unreadable: {error})"
    return "\n".join(text.splitlines()[-lines:]) or f"({path} is empty)"


RUN_ROOT_ENV = "VIVARIUM_RUN_ROOT"
"""A short directory `vivarium run` removes however it ends; see
`vivarium/runroot.py`."""


def short_tmp() -> str:
    """Where a socket path short enough for `sockaddr_un` goes: the run
    root when there is one, which is cleaned up; else /tmp."""
    return os.environ.get(RUN_ROOT_ENV) or "/tmp"


def memory_bytes(memory: str) -> int:
    """``mem=`` as a number, the way UML's ``memparse`` reads it."""
    scale = {"k": 1024, "m": 1024**2, "g": 1024**3}
    if memory[-1:].lower() in scale:
        return int(memory[:-1]) * scale[memory[-1].lower()]
    return int(memory)


def memory_file_kib(pid: int, memory: str) -> int:
    """Kibibytes allocated to the guest's RAM file, which process *pid* holds open.

    Unlinked and exactly *memory* long. QEMU also holds its scratch disk
    by fd and unlinked, so a `memfd:` target wins over a bare size match
    -- a qcow2 that happened to be exactly this long would otherwise be
    read as the guest's memory.
    """
    want = memory_bytes(memory)
    found: int | None = None
    for entry in (Path("/proc") / str(pid) / "fd").iterdir():
        try:
            target = str(entry.readlink())
            if not target.endswith("(deleted)"):
                continue
            stat = entry.stat()
        except OSError:
            continue
        if stat.st_size != want:
            continue
        if "memfd:" in target:
            return stat.st_blocks // 2
        found = stat.st_blocks // 2
    if found is not None:
        return found
    raise BackendError(f"found no memory file under /proc/{pid}/fd")


def processes_pss_kib(pid: int) -> int:
    """`container.tree_pss_kib`, raising `BackendError`."""
    try:
        return container.tree_pss_kib(pid)
    except RuntimeError as error:
        raise BackendError(str(error)) from error


def socket_dir(rundir: Path, name: str, fallback: str | None) -> tuple[Path, list[Path]]:
    """A directory to put *name* in where the kernel will accept the path.

    *rundir* where it fits, and a directory of its own where it does not:
    a unix socket's path goes in a ``sockaddr_un``, which holds 108 bytes,
    and a caller whose TMPDIR is long -- an agent's scratch directory is
    often 90 characters on its own -- would otherwise lose the control
    channel with nothing but a log line about it.

    Returns the directory and whatever has to be removed afterwards.
    """
    if unixpath.fits(rundir / name):
        return rundir, []
    made = Path(tempfile.mkdtemp(prefix="vivarium-", dir=fallback or short_tmp()))
    return made, [made]


def require_ids(name: str, kind: str) -> None:
    """Refuse a guest whose file server needs the namespace's subordinate ids.

    virtiofsd and libkrun's virtio-fs serve a file as the guest user that
    made it, so a guest user outside the namespace's ids gets EINVAL on its
    first write to /artifacts, minutes in.
    """
    if not container.owns_ids():
        raise BackendError(
            f"{name}: a {kind} guest needs {container.SUBORDINATE_IDS} ids "
            "in the run's namespace, and it maps fewer\n"
            "    fix: give your user subordinate ids in /etc/subuid and /etc/subgid, "
            "with newuidmap and newgidmap installed; a Nix build asks for uid-range"
        )


def abandon(helpers: Iterable[subprocess.Popen[bytes]], opened: Iterable[int]) -> None:
    """Kill the helpers and close the fds of a launch that failed part way.

    Nothing owns these until a Launch carries them back, so a failure
    between the first one and the last would leave a daemon running, or an
    unlinked disk image alive with no name and no way to find it. A test
    that fails while booting is exactly when that happens.
    """
    for helper in helpers:
        if helper.poll() is None:
            helper.kill()
            helper.wait()
    for fd in opened:
        os.close(fd)


PULSE = 0.05
"""How long a helper that fails at startup takes to fail. virtiofsd and
swtpm fail at once or not at all."""


async def alive(proc: subprocess.Popen[bytes]) -> bool:
    """Whether *proc* still runs a `PULSE` after its start: the readiness
    check of a helper that says nothing when it is ready."""
    await anyio.sleep(PULSE)
    return proc.poll() is None


RELAY_IN_PORT = 5001
"""Where the router's relay takes forwards in: its IN_PORT, in modules/router.nix."""


def egress_args(machine: Machine) -> list[str]:
    """The egress's options for *machine*'s uplink."""
    return forward.egress_uplink_args(machine.spec.uplink_network, machine.offline)


def egress_adds(machine: Machine, relay: str) -> list[forward.Add]:
    """The sockets *machine*'s egress serves, whose relay is at *relay*: a
    router's for each of its guests, a `vsock` guest's for itself."""
    guests = machine.routed if machine.spec.router else (machine,)
    return [
        add
        for guest in guests
        if (address := guest.uplink_ip) is not None
        for add in forward.egress_adds(guest.claims, address, relay)
    ]


EGRESS_ANSWER = 10.0
"""Seconds the egress has to answer one control message."""


def egress_exchange(control: socket.socket, messages: Sequence[forward.Add | forward.Remove]) -> None:
    """Send the egress each message, and wait for its answer: the next
    goes only after it, so one read on the far side is one message.
    Blocking; a caller on the event loop runs it in a thread."""
    control.settimeout(EGRESS_ANSWER)
    try:
        for message in messages:
            said = message.line().decode().strip()
            socket.send_fds(control, [message.line()], message.fds())
            answer = b""
            while not answer.endswith(b"\n"):
                chunk = control.recv(512)
                if not chunk:
                    raise BackendError(f"the egress ended before it answered {said!r}")
                answer += chunk
            if answer != b"ok\n":
                raise BackendError(f"the egress refused {said!r}: {answer.decode().strip()}")
    except TimeoutError as error:
        raise BackendError(f"the egress did not answer in {EGRESS_ANSWER:g}s") from error
    finally:
        control.settimeout(None)


async def start_egress(
    egress: Path | None,
    rundir: Path,
    gateway: str,
    listen: str,
    ready: Path,
    args: list[str],
    adds: list[forward.Add],
    helpers: list[subprocess.Popen[bytes]],
) -> socket.socket:
    """Start the router's egress on *listen*, into *helpers*, wait until
    *ready* exists, and hand it *adds*: the control socket.

    The egress maps the uplink's *gateway* to the host's loopback. It ends
    when the control socket closes, so the caller keeps it for as long as
    the egress serves.
    """
    if egress is None:
        raise BackendError("the router needs egress in vivarium.launch.config")
    log = rundir / "egress.log"
    control, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        with child, log.open("wb") as handle:
            proc = subprocess.Popen(
                [str(egress), listen, gateway, *args, f"--control={child.fileno()}"],
                stdin=subprocess.DEVNULL,
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                preexec_fn=die_with_parent,
                pass_fds=(child.fileno(),),
            )
        helpers.append(proc)
        # A poll: the egress says nothing when it listens.
        with anyio.move_on_after(10) as waited:
            while not ready.exists():
                if proc.poll() is not None:
                    raise BackendError(f"the egress exited ({proc.returncode}):\n{log_tail(log)}")
                await anyio.sleep(0.02)
        if waited.cancelled_caught:
            raise BackendError(f"the egress did not listen on {ready}:\n{log_tail(log)}")
        try:
            await anyio.to_thread.run_sync(functools.partial(egress_exchange, control, adds))
        except BackendError as error:
            raise BackendError(f"{error}\n{log_tail(log)}") from error
    except BaseException:
        control.close()
        raise
    return control


KEPT_SERIAL: Final = "vivarium-kept"
"""The serial a QEMU or krun guest finds its kept disk by: `modules/kept.nix`."""


def kept_store(machine: Machine, rundir: Path, *, directory: bool = False) -> Path | None:
    """`vivarium.persist`'s store of *machine*: `make_kept_store` for its spec."""
    return make_kept_store(machine.spec.keep, machine.kept, rundir, directory=directory)


def make_kept_store(size: int | None, kept: Path | None, rundir: Path, *, directory: bool = False) -> Path | None:
    """`vivarium.persist`'s store on the host, made when missing: a sparse
    raw file of *size* MiB, or a *directory* for a container guest.
    *kept*, the environment's, else one in *rundir* that lasts the run.
    None for a guest that keeps nothing (*size* None).

    By path, not an unlinked fd as `scratch_disk`: it must outlive the
    run. A file an earlier run made keeps its size, because the guest's
    ext4 fills the disk it was made on."""
    if size is None:
        return None
    path = kept or rundir / "kept"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # A container guest's store is a directory and every other a disk, and
    # neither turns into the other: the data stays with the backend.
    if path.exists() and path.is_dir() != directory:
        made, wanted = ("a directory", "a disk") if path.is_dir() else ("a disk", "a directory")
        raise BackendError(
            f"{path} is {made}, made by an earlier run on another backend, and this backend keeps {wanted};"
            " run the environment on that backend, or `vivarium rm` it"
        )
    if directory:
        path.mkdir(mode=0o700, exist_ok=True)
        return path
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if os.fstat(fd).st_size == 0:
            os.ftruncate(fd, size << 20)
    finally:
        os.close(fd)
    return path


async def scratch_disk(qemu_img: Path, rundir: Path, image: Path, backing: str = "raw") -> tuple[int, int]:
    """A writable layer over the read-only root image, with no name.

    The same shape as UML's ``ubd0=<cow>,<image>``: the image stays in
    the store, and everything the guest writes goes here instead.

    The file is unlinked before QEMU is started and handed over as open
    file descriptors.  So the only thing keeping those gigabytes alive
    is a process, and the kernel frees them the moment QEMU is gone --
    including when the runner is killed with a signal it cannot catch,
    which no amount of cleanup code covers.

    Two descriptors, read-write and read-only, because QEMU opens an
    image O_RDONLY to probe its format before reopening it O_RDWR, and
    picks from the fd set by access mode.  With one it says
    ``Failed to find file descriptor with matching flags=0x0``.
    """
    scratch = rundir / "root.qcow2"
    done = await anyio.run_process(
        [
            str(qemu_img),
            "create",
            "-q",
            "-f",
            "qcow2",
            # Named, because qemu-img refuses to guess a backing
            # format and a guess would be silently wrong. Raw for a
            # NixOS root and CHR; qcow2 for an image that ships one,
            # such as SONiC's.
            "-F",
            backing,
            "-b",
            str(image),
            str(scratch),
        ],
        check=False,
    )
    if done.returncode != 0:
        said = (done.stderr or done.stdout).decode(errors="replace").strip()
        raise BackendError(f"could not make a scratch disk over {image}: {said}")
    fds = (os.open(scratch, os.O_RDWR), os.open(scratch, os.O_RDONLY))
    scratch.unlink()
    return fds
