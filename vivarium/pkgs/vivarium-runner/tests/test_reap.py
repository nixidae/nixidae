"""`Machine._reap`: the guest's own pid goes down with the process group, even outside it."""

from __future__ import annotations

import contextlib
import os
import signal
import sys

# Machine._process is asyncio's process.
from asyncio import subprocess  # noqa: TID251 -- see above
from pathlib import Path

import anyio
from vivarium_runner import Machine

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


class Reaped(Machine):
    """A machine with no guest: `_reap` alone, over a process the test starts."""

    def __init__(self, pid_file: Path) -> None:
        self._pid_file = pid_file

    @property
    def name(self) -> str:
        return "one"


def test_the_guest_pid_outside_the_group_is_reaped(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    vm = Reaped(pid_file)

    async def main() -> None:
        vm._process = await subprocess.create_subprocess_exec(
            sys.executable, "-c", ESCAPE, str(pid_file), stdout=subprocess.PIPE, start_new_session=True
        )
        assert vm._process.stdout is not None
        assert await vm._process.stdout.readline() == b"ready\n"
        guest = int(pid_file.read_text())
        watch = os.pidfd_open(guest)
        try:
            await vm._reap(0)
            with anyio.fail_after(5):
                await anyio.wait_readable(watch)
        finally:
            os.close(watch)
            with contextlib.suppress(ProcessLookupError):
                os.kill(guest, signal.SIGKILL)

    anyio.run(main)
