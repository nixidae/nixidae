#!/usr/bin/env python3
"""Kill the runner before it writes `status`: the check must still fail."""

from __future__ import annotations

import os
import signal

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    del vms
    os.kill(os.getpid(), signal.SIGKILL)
