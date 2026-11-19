"""`GuestProcess`: what one boot leaves on the host goes down with it."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess as sync_subprocess
import sys

# GuestProcess wraps asyncio's process.
from asyncio import subprocess  # noqa: TID251 -- see above
from pathlib import Path

import anyio
import pytest
from vivarium_runner.backends import Launch
from vivarium_runner.guestprocess import GuestProcess

# The guest forks out of the group, as a UML kernel under its bridge does
# with `setsid`, and says so once its pid file is written.
ESCAPE = """
import os, sys, time
read, write = os.pipe()
if os.fork() == 0:
    os.setsid()
    with open(sys.argv[1], "w") as pid_file:
        pid_file.write(str(os.getpid()))
    os.close(write)
    time.sleep(60)
    os._exit(0)
os.close(write)
os.read(read, 1)
print("ready", flush=True)
time.sleep(60)
"""


@pytest.mark.skipif(
    not hasattr(os, "pidfd_open"), reason="waits on a process that is not its child through a pidfd: Linux only"
)
def test_the_guest_pid_outside_the_group_is_reaped(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"

    async def main() -> None:
        process = await subprocess.create_subprocess_exec(
            sys.executable, "-c", ESCAPE, str(pid_file), stdout=subprocess.PIPE, start_new_session=True
        )
        assert process.stdout is not None
        assert await process.stdout.readline() == b"ready\n"
        spawned = GuestProcess(process, pid_file=pid_file)
        guest = int(pid_file.read_text())
        watch = os.pidfd_open(guest)
        try:
            assert await spawned.reap(0)
            with anyio.fail_after(5):
                await anyio.wait_readable(watch)
        finally:
            os.close(watch)
            with contextlib.suppress(ProcessLookupError):
                os.kill(guest, signal.SIGKILL)

    anyio.run(main)


def test_a_spawn_that_fails_releases_the_helpers(tmp_path: Path) -> None:
    helper = sync_subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)
    made = tmp_path / "made"
    made.mkdir()
    launch = Launch(argv=[str(tmp_path / "missing")], pass_fds=(), helpers=[helper], cleanup=[made])
    try:
        with pytest.raises(FileNotFoundError):
            anyio.run(GuestProcess.spawn, launch)
        assert helper.poll() is not None
        assert not made.exists()
    finally:
        if helper.poll() is None:
            helper.kill()
            helper.wait()
