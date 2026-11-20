"""One boot of a guest, as host processes: what the runner spawned, the
helpers its backend started, and the guest's own pid."""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess as sync_subprocess

# asyncio's subprocess for the spawn alone: anyio's `open_process` takes no
# `preexec_fn`, and nothing else in anyio sets the death signal in the child
# (Lillecarl/anyio#8). Its stdout reaches the rest as an anyio stream.
from asyncio import subprocess  # noqa: TID251 -- see above
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from anyio.abc import ByteReceiveStream

from .lifetime import die_with_parent

if TYPE_CHECKING:
    import asyncio

    from .backends import Launch


def _killpg(pid: int, sig: int) -> None:
    """Signal *pid*'s whole process group, and tolerate it being gone.

    The group and not the process: a guest is a tree -- a UML kernel runs
    its userspace as host processes under it -- and signalling only the
    leader leaves the rest running with nothing to report to.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), sig)


def _running(pid: int) -> bool:
    """*pid* exists and is not a zombie waiting to be reaped."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    # The state follows the command name, which may itself hold ") ".
    return stat.rpartition(")")[2].split()[0] not in ("Z", "X")


class _Stdout(ByteReceiveStream):
    """The spawned process's stdout, as anyio's stream."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self._reader = reader

    async def receive(self, max_bytes: int = 65536) -> bytes:
        data = await self._reader.read(max_bytes)
        if not data:
            raise anyio.EndOfStream
        return data

    async def aclose(self) -> None:
        """The pipe closes with the process."""


class GuestProcess:
    """The host processes of one boot of a guest."""

    def __init__(
        self,
        process: subprocess.Process,
        *,
        helpers: list[sync_subprocess.Popen[bytes]] | None = None,
        pid_file: Path | None = None,
        cleanup: list[Path] | None = None,
    ) -> None:
        self._process = process
        self.stdout: ByteReceiveStream | None = None if process.stdout is None else _Stdout(process.stdout)
        """The guest's console, from its spawned process."""
        self._helpers = helpers or []
        self.pid_file = pid_file
        """Where the guest wrote its own pid, when that is not `pid`."""
        self._cleanup = cleanup or []

    @classmethod
    async def spawn(cls, launch: Launch) -> GuestProcess:
        """Start *launch*'s argv. The result owns *launch*'s helpers and
        directories; a start that fails releases them."""
        try:
            process = await subprocess.create_subprocess_exec(
                *launch.argv,
                # Never the runner's stdin: a UML console reads it, and took
                # the input meant for `--interactive`'s REPL (measured).
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=launch.env or None,
                # Own process group, so everything the backend started under
                # it dies together when it is signalled.
                start_new_session=True,
                # And the kernel kills it if the runner never gets to signal anything.
                preexec_fn=die_with_parent,
                pass_fds=launch.pass_fds,
            )
        except BaseException:
            _release(launch.helpers, launch.cleanup)
            raise
        return cls(process, helpers=launch.helpers, pid_file=launch.pid_file, cleanup=launch.cleanup)

    @property
    def pid(self) -> int:
        """The process the runner spawned."""
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    async def wait(self) -> int:
        """Until the spawned process has exited; its exit code."""
        return await self._process.wait()

    def guest_pid(self) -> int | None:
        """The guest's own process: the kernel under UML, QEMU itself."""
        if self.pid_file is None:
            return self._process.pid
        try:
            return int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            return None

    def alive(self) -> bool:
        """Is the guest's own process still running?"""
        pid = self.guest_pid()
        return pid is not None and _running(pid)

    def signal(self, sig: int) -> None:
        """Signal the process group, and the guest's own pid as well.

        The pid too because under UML the spawned process is the bridge,
        and killing its group alone left the kernel answering RPC in one
        run of two.
        """
        pid = self.guest_pid()
        if self._process.returncode is None:
            _killpg(self._process.pid, sig)
        if pid is not None and pid != self._process.pid and _running(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)

    async def reap(self, grace: float, *, kill: bool = False) -> bool:
        """Wait *grace* seconds for the process to exit, then SIGTERM its
        group, then SIGKILL it. *kill* goes to SIGKILL at once.

        False when it is still there after all of them.
        """
        if self._process.returncode is not None:
            return True
        # The agent's poweroff returns once the shutdown is queued, so a
        # guest that was asked gets the time to finish it: measured, a
        # QEMU guest was SIGTERMed right after the request.
        if grace:
            with anyio.move_on_after(grace):
                await self._process.wait()
                return True
        steps = ((signal.SIGKILL, 5),) if kill else ((signal.SIGTERM, 30), (signal.SIGKILL, 5))
        for sig, wait in steps:
            self.signal(sig)
            with anyio.move_on_after(wait):
                await self._process.wait()
                return True
        return False

    def release(self) -> None:
        """Kill the helpers and remove the backend's directories."""
        _release(self._helpers, self._cleanup)
        self._helpers = []
        self._cleanup = []


def _release(helpers: list[sync_subprocess.Popen[bytes]], cleanup: list[Path]) -> None:
    """Kill *helpers* and remove *cleanup*.

    By group and not by pid: each helper leads its own session, and a
    helper that forked leaves the child behind when only the leader is
    killed.
    """
    for helper in helpers:
        if helper.poll() is None:
            _killpg(helper.pid, signal.SIGKILL)
            helper.wait()
    for directory in cleanup:
        shutil.rmtree(directory, ignore_errors=True)
