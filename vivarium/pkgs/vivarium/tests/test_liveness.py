from __future__ import annotations

import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

import pytest
from vivarium import liveness
from vivarium.liveness import Life

if TYPE_CHECKING:
    from pathlib import Path


def test_no_lock_is_no_answer(tmp_path: Path) -> None:
    assert liveness.life(tmp_path) is Life.ABSENT


def test_a_held_lock_is_a_live_run_and_a_closed_one_an_ended_run(tmp_path: Path) -> None:
    fd = liveness.hold(tmp_path)
    assert liveness.life(tmp_path) is Life.HELD
    with pytest.raises(liveness.RunLive):
        liveness.hold(tmp_path / ".." / tmp_path.name, environ={})
    os.close(fd)
    assert liveness.life(tmp_path) is Life.FREE


def test_forget_removes_only_an_ended_runs_lock(tmp_path: Path) -> None:
    fd = liveness.hold(tmp_path)
    liveness.forget(tmp_path)
    assert liveness.life(tmp_path) is Life.HELD
    os.close(fd)
    liveness.forget(tmp_path)
    assert liveness.life(tmp_path) is Life.ABSENT


def test_the_runner_adopts_the_lock_its_cli_handed_down(tmp_path: Path) -> None:
    environ: dict[str, str] = {}
    fd = liveness.hold(tmp_path, environ)
    liveness.hand_down(fd, environ)
    assert os.get_inheritable(fd)
    assert liveness.hold(tmp_path, environ) == fd
    assert liveness.ENV not in environ
    assert not os.get_inheritable(fd)
    os.close(fd)


def test_a_descriptor_of_another_file_is_not_adopted(tmp_path: Path) -> None:
    other = os.open(tmp_path / "other", os.O_RDWR | os.O_CREAT, 0o600)
    environ = {liveness.ENV: str(other)}
    fd = liveness.hold(tmp_path / "out", environ)
    assert fd != other
    assert not os.get_inheritable(other)
    assert liveness.life(tmp_path / "out") is Life.HELD
    os.close(fd)
    os.close(other)


# The runner, as a process: it takes the lock and starts a child that
# outlives it, as a guest launcher would.
RUNNER = """
import subprocess, sys, time
from pathlib import Path
from vivarium import liveness
liveness.hold(Path(sys.argv[1]))
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
print(child.pid, flush=True)
time.sleep(60)
"""


def test_sigkill_frees_the_lock_and_no_child_keeps_it(tmp_path: Path) -> None:
    runner = subprocess.Popen(  # noqa: S603 -- this interpreter, with the script above
        [sys.executable, "-c", RUNNER, str(tmp_path)], stdout=subprocess.PIPE, text=True
    )
    if runner.stdout is None:
        raise AssertionError("no pipe")
    child = int(runner.stdout.readline())
    try:
        assert liveness.life(tmp_path) is Life.HELD
        runner.send_signal(signal.SIGKILL)
        runner.wait()
        os.kill(child, 0)
        assert liveness.life(tmp_path) is Life.FREE
    finally:
        os.kill(child, signal.SIGKILL)
