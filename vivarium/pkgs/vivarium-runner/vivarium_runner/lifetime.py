"""A child process that ends with the runner. `backend` and `container` both spawn with it."""

from __future__ import annotations

import ctypes
import os
import signal
import sys

PR_SET_PDEATHSIG = 1
"""``prctl`` option number, which Python's ``signal`` module does not
carry.  From ``include/uapi/linux/prctl.h``; the number is ABI."""


def die_with_parent() -> None:
    """Ask the kernel to SIGKILL this child when the runner dies.

    Every other guarantee here is cleanup code, and cleanup code does not
    run when the runner is killed with SIGKILL, dies on a fault, or is
    stopped by a build that gave up.  A guest that outlives its runner is
    then a UML kernel or a QEMU spinning on a core with nothing left to
    report to -- measured, one such guest used a whole core for 31 hours.

    ``PR_SET_PDEATHSIG`` is the only thing that covers that case: the
    kernel sends the signal, so nothing has to be running to send it.

    Called after ``setsid``, which does not clear it.  The ``getppid``
    check closes the race where the runner dies between the fork and this
    call, which would otherwise leave the child holding a death signal
    that can no longer arrive.

    macOS has no such signal: there a killed runner leaves its guests up.
    """
    if sys.platform != "linux":
        return
    parent = os.getppid()
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, signal.SIGKILL)
    if os.getppid() != parent:
        os._exit(1)
