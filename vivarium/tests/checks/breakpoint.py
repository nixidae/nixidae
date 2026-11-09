"""Can a person reach into a paused run, and end it?

Driven with the real binaries, the way a person or an agent drives them:
the driver with `--break` in the background, `vivarium ctl` against its
socket. The file injected and the tests sent by hand are written here,
in the build directory, and were never in the store: that is the point
of `inject` and `ctl pytest`.

- paused before `later`: `state` says why, `exec`, a namespace that
  survives, an exception as the reply, `inject`, `pytest` by hand and
  then its edit, `continue`;
- `--break end` pauses after the last phase;
- `ctl stop` ends a paused run: it returns once the run has exited, the
  run exits 5, the verdict says stopped, and no guest process is left.

    breakpoint VIVARIUM DRIVER WORKDIR
"""

import os
import subprocess
import sys
from pathlib import Path

from harness import UML_PROCESSES, Run, data, running

STOPPED = 5

INJECTED = """\
from vivarium_runner import Machines

async def test(vms: Machines) -> None:
    await vms.one.succeed("echo injected > /artifacts/injected")
    print("[scratch] wrote it")
"""

BY_HAND = 'async def test_host(one): assert (await one.succeed("hostname")).strip() == "{name}"\n'


def by_hand(vivarium: str, driver: str, work: Path) -> None:
    run = Run(vivarium, driver, work / "run", "--break", "later")
    paused = run.wait_for_pause()
    if (paused["state"] or {}).get("later") != "pending":
        run.fail("the phase ran through its breakpoint")
    if paused["pause"] != "before later":
        run.fail(f"state does not say why the run paused: {paused}")
    if run.ok("state").splitlines()[0] != "paused before later":
        run.fail("ctl state does not print the pause reason")
    print("ok: paused before later, and state says so")

    if "one" not in run.ok("exec", 'await one.succeed("hostname")'):
        run.fail("exec did not reach the guest")
    print("ok: exec reached the guest")

    run.ok("exec", 'n = (await one.succeed("echo 7")).strip()')
    if run.ok("exec", "n").strip() != "'7'":
        run.fail("a name did not survive to the next exec")
    print("ok: the namespace survives between calls")

    raised = run.ctl("exec", "1/0")
    if raised.returncode == 0:
        run.fail("an exception was reported as success")
    if "ZeroDivisionError" not in raised.stderr:
        run.fail(f"the traceback did not come back: {raised.stderr}")
    print("ok: an exception comes back as the reply, and the run stays up")

    scratch = work / "scratch.py"
    scratch.write_text(INJECTED)
    if "wrote it" not in run.ok("inject", str(scratch)):
        run.fail("inject did not run the file")
    print("ok: a file from outside the store ran against the guest")

    tree = work / "tree"
    tree.mkdir()
    (tree / "test_by_hand.py").write_text(BY_HAND.format(name="one"))
    if "1 passed" not in run.ok("pytest", str(tree)).splitlines():
        run.fail("pytest by hand did not pass against the guest")
    (tree / "test_by_hand.py").write_text(BY_HAND.format(name="edited"))
    if run.ctl("pytest", str(tree), "--", "-k", "host").returncode == 0:
        run.fail("pytest by hand ran the old test after an edit")
    print("ok: pytest ran a working tree's test, then its edit, against the paused guest")

    run.ok("continue")
    run.wait("after continue")
    print("ok: continue finished the run")

    if run.phase_states().get("later") != "passed":
        run.fail("later did not run after continue")
    if not (run.out / "artifacts" / "one" / "injected").is_file():
        run.fail("the injected write is not on the host")
    events = run.events()
    if not any(e["kind"] == "output" and e.get("phase") == "inject:scratch.py" for e in events):
        run.fail("what the injected file printed is not in the events")
    if not any(e["kind"] == "note" and data(e).get("op") == "exec" for e in events):
        run.fail("the exec is not recorded in the events")
    print("ok: and events.jsonl records what was done by hand")

    if (run.out / "status").read_text().strip() != "0":
        run.fail("a failing test sent by hand failed the run")
    if "test_by_hand" in (run.out / "junit.xml").read_text():
        run.fail("a test sent by hand is in junit.xml")
    if not any(e["kind"] == "case" and data(e).get("by_hand") and data(e).get("outcome") == "failed" for e in events):
        run.fail("the by-hand case is not in the events")
    print("ok: and its cases are events, outside the verdict and junit.xml")
    if (run.out / "control.sock").exists():
        run.fail("the socket outlived the run")


def break_at_end(vivarium: str, driver: str, work: Path) -> None:
    """A point, not a phase: `end` pauses after the last phase."""
    run = Run(vivarium, driver, work / "end", "--break", "end")
    if (run.wait_for_pause()["state"] or {}).get("later") != "passed":
        run.fail("--break end paused before the last phase ran")
    run.ok("continue")
    run.wait("after continue at end")
    print("ok: --break end paused after the last phase")


def stop(vivarium: str, driver: str, work: Path) -> None:
    run = Run(vivarium, driver, work / "stop", "--break", "later")
    run.wait_for_pause()
    if run.ok("stop").strip() != "stopped":
        run.fail("ctl stop did not wait for the run to end")
    if run.process.poll() is None:
        run.fail("the run is still up after ctl stop returned")
    if run.process.returncode != STOPPED:
        run.fail(f"a stopped run exited {run.process.returncode}, not {STOPPED}")
    if run.phases()["stopped"] != "ctl stop":
        run.fail("phases.json does not say stopped")
    if (run.out / "status").read_text().strip() != "1":
        run.fail("a stopped run reads as passed")
    if not any(e["kind"] == "run_finished" and data(e).get("stopped") == "ctl stop" for e in run.events()):
        run.fail("the verdict event does not say stopped")
    if (run.out / "control.sock").exists():
        run.fail("the socket outlived a stopped run")
    if left := running(UML_PROCESSES):
        run.fail(f"guest processes are left after stop: {', '.join(left)}")
    monitor = subprocess.run(
        [vivarium, "monitor", str(run.out), "--quiet", "--wait", "5"], capture_output=True, text=True, timeout=60
    )
    if monitor.returncode != STOPPED:
        run.fail(f"the monitor read a stopped run as {monitor.returncode}, not {STOPPED}: {monitor.stdout}")
    print("ok: ctl stop tore the guests down and the run says stopped")


def main() -> None:
    vivarium, driver, work = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    os.environ.setdefault("HOME", str(work))
    by_hand(vivarium, driver, work)
    break_at_end(vivarium, driver, work)
    stop(vivarium, driver, work)


if __name__ == "__main__":
    main()
