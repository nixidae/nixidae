"""macOS's sysctl values, by name.

`sysctlbyname(3)` and not `/usr/sbin/sysctl`: the darwin build sandbox
refuses to run that program, and reading the value is allowed.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os


def read(name: str) -> int:
    """The integer sysctl *name*, 32 or 64 bits wide."""
    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    value = ctypes.c_uint64(0)
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if libc.sysctlbyname(name.encode(), ctypes.byref(value), ctypes.byref(size), None, ctypes.c_size_t(0)) != 0:
        errno = ctypes.get_errno()
        raise OSError(errno, f"sysctl {name}: {os.strerror(errno)}")
    if size.value == 4:
        return value.value & 0xFFFFFFFF
    return value.value
