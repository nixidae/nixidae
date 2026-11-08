"""Which ended guest process asked for a reboot, per backend.

`checks.reboot` and `checks.reboot.krun` prove the whole path on a guest;
this holds the decision each backend makes from what the process left.
"""

from __future__ import annotations

import pytest
from vivarium_runner.backend import REBOOTED, Container, Krun, KrunExit, Qemu, Uml

POWER_DOWN = "[   12.3] reboot: Power down"
RESTART = f"[   12.3] {REBOOTED}"


@pytest.mark.parametrize(
    ("returncode", "rebooted"),
    [
        (KrunExit.REBOOT, True),
        (KrunExit.POWEROFF, False),
        (KrunExit.CRASH, False),
        (-9, False),
        (None, False),
    ],
)
def test_krun_reads_the_exit_code(returncode: int | None, rebooted: bool) -> None:
    # Every krun stop resets and prints the restart line, so the console decides nothing.
    assert Krun.rebooted(returncode, [RESTART]) is rebooted


@pytest.mark.parametrize(("console", "rebooted"), [([RESTART], True), ([POWER_DOWN], False), ([], False)])
def test_qemu_reads_the_console(console: list[str], rebooted: bool) -> None:
    assert Qemu.rebooted(0, console) is rebooted


def test_the_backends_that_do_not_reboot_say_no() -> None:
    for backend in (Uml, Container):
        assert backend.rebooted(KrunExit.REBOOT, [RESTART]) is False
