"""Runaway recursion on an async evaluator raises; it does not crash.

Nix's default `max-call-depth` needs about 27 MB of C stack, and a thread
the runtime starts would otherwise keep the 8 MiB `RLIMIT_STACK` gives it.
Then the stack runs out before Nix's counter fires, and the process takes
SIGSEGV. A child process runs the recursion, so a regression fails this
test instead of taking the suite down with it.

ASAN's frames are larger: 6000 calls fit the 60 MiB stack and 8000 do
not. A bigger stack does not help: from 90 MiB to 240 MiB, ASAN
declines to clear the stack after the throw and then fails its own
check (google/sanitizers#189). So under ASAN the child lowers the depth, and
the test still proves that recursion raises rather than crashes.
"""

import subprocess
import sys

from nixversion import STACK_OVERFLOW, UNDER_ASAN

ASAN_DEPTH = 3000

CHILD = """
import sys
import anyio
from huggorm import AsyncSession
from huggorm.errors import NixError

async def main():
    async with AsyncSession("dummy://") as session:
        state = session.eval(session.store())
        if len(sys.argv) > 1:
            await state.set_setting("max-call-depth", sys.argv[1])
        try:
            await state.eval_expr("let f = n: f (n + 1); in f 0")
        except NixError as e:
            print("raised:", e)

anyio.run(main)
"""


def test_runaway_recursion_raises_rather_than_crashing() -> None:
    depth = [str(ASAN_DEPTH)] if UNDER_ASAN else []
    done = subprocess.run([sys.executable, "-c", CHILD, *depth], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, (done.returncode, done.stderr[-2000:])
    assert "raised:" in done.stdout and STACK_OVERFLOW in done.stdout, done.stdout
