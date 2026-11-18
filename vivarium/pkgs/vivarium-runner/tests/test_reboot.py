"""Which ended guest process asked for a reboot, per backend.

`checks.reboot` and `checks.reboot.krun` prove the whole path on a guest;
this holds the decision each backend makes from how the process ended.
"""

from __future__ import annotations

import pytest
from vivarium_runner.backends import Stop
from vivarium_runner.backends.container import Container
from vivarium_runner.backends.krun import Krun, KrunExit
from vivarium_runner.backends.qemu import Qemu
from vivarium_runner.backends.uml import Uml


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
    assert Krun.rebooted(Stop(returncode)) is rebooted


@pytest.mark.parametrize(
    ("cause", "rebooted"),
    [("guest-reset", True), ("guest-shutdown", False), ("host-signal", False), (None, False)],
)
def test_qemu_reads_the_shutdown_cause(cause: str | None, rebooted: bool) -> None:
    # A reset before Linux, from the firmware or the bootloader, prints no
    # console line, so the console decides nothing.
    assert Qemu.rebooted(Stop(0, cause)) is rebooted


def test_the_backends_that_do_not_reboot_say_no() -> None:
    for backend in (Uml, Container):
        assert backend.rebooted(Stop(KrunExit.REBOOT, "guest-reset")) is False
