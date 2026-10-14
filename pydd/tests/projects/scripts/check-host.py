"""psutil comes from the derivation that a host function made, and it runs."""

import sys
from pathlib import Path

import cffi
import psutil

marker = Path(psutil.__file__).parents[1] / "pydd_host_built"
print(f"psutil       {psutil.__version__} cpus={psutil.cpu_count()} from {Path(psutil.__file__).parents[1]}")
print(f"cffi         {cffi.__version__} sizeof(long)={cffi.FFI().sizeof('long')}")
if not marker.exists():
    print(f"no {marker.name} beside psutil, so the host function did not build it")
    sys.exit(1)
print("psutil is the node that the host function made")
