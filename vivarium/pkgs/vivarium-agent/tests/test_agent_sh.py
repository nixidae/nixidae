"""The agent's `_sh`: a command past its timeout takes its whole process group with it."""

from __future__ import annotations

import os
import shlex
import signal
import sys
import time
from pathlib import Path

import pytest
from vivarium_agent import agent


@pytest.fixture(autouse=True)
def host_path(monkeypatch: pytest.MonkeyPatch) -> None:
    # The guest's PATH is not on this host.
    monkeypatch.setattr(agent, "GUEST_PATH", os.environ["PATH"])


def running(pid: int) -> bool:
    """Whether *pid* runs: a zombie waits only for a parent that reaps it."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return False
    return stat.rpartition(")")[2].split()[0] not in ("Z", "X")


def test_a_command_returns_its_status_and_output() -> None:
    done = agent._sh("echo out; echo err >&2; exit 3", timeout=5)
    assert (done.returncode, done.stdout, done.stderr) == (3, "out\n", "err\n")


def test_a_timeout_kills_the_child_in_the_background() -> None:
    started = time.monotonic()
    with pytest.raises(TimeoutError) as raised:
        agent._sh("sleep 60 >/dev/null 2>&1 & echo $!; exec sleep 60 >/dev/null", timeout=0.5)
    assert time.monotonic() - started < agent.KILL_WAIT
    message, _, pid = str(raised.value).partition("\n")
    assert message == "ran past its 0.5s timeout; its process group got SIGKILL"
    assert not running(int(pid))


def test_a_process_that_left_the_group_bounds_the_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(agent, "KILL_WAIT", 0.2)
    pidfile = tmp_path / "pid"
    escape = f"import os, time; os.setsid(); open({str(pidfile)!r}, 'w').write(str(os.getpid())); time.sleep(60)"
    try:
        with pytest.raises(TimeoutError, match=r"a process outside the group held it open past 0\.2s"):
            agent._sh(f"{sys.executable} -c {shlex.quote(escape)} & wait", timeout=0.5)
    finally:
        if pidfile.exists():
            os.kill(int(pidfile.read_text()), signal.SIGKILL)
