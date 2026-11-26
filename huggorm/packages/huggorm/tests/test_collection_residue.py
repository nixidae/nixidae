"""A closed session's evaluation is freed by the next collection.

A collection left mark stack entries on the thread that ran it, in the
frames below it and in its vector registers, and the next collection
kept what they pointed at. A loop of record-mode evaluations kept one
or more of them, at random (nixidae/nixidae#1624). Measured on this
test before boehmgc-clear-collection-residue.patch: 5 of 5 runs failed,
each keeping 8 to 15 MiB from some round on; after it, 5 of 5 passed
with every round at 4 MiB.

In a child: the measure is process-wide.
"""

import pathlib
import subprocess
import sys

import pytest
from nixversion import needs_collector

CHILD = """
import gc
import sys
import anyio
from huggorm import AsyncSession
from huggorm_bindings import collect_garbage, gc_stats

EXPR = '''
let
  drv = i: {
    type = "derivation";
    name = "d${toString i}";
    outPath = "/nix/store/${builtins.substring 0 32 (builtins.hashString "sha256" (toString i))}-d${toString i}";
    deps = builtins.genList (j: { n = i * 7 + j; s = "x${toString j}"; }) 40;
  };
in
builtins.stringLength (builtins.concatStringsSep " " (map (d: "${d}") (builtins.genList drv 60000)))
'''

async def main():
    for _ in range(10):
        async with AsyncSession(sys.argv[1]) as session:
            state = session.eval(session.store())
            await state.set_record_derivations(True)
            await (await state.eval_expr(EXPR, "/")).to_json()
        gc.collect()
        await anyio.to_thread.run_sync(collect_garbage)
        print(gc_stats()["used_bytes"] >> 20, flush=True)

anyio.run(main)
"""


@pytest.mark.skipif(sys.platform != "linux", reason="measured on Linux only")
@needs_collector
def test_a_closed_evaluation_is_collected(tmp_path: pathlib.Path) -> None:
    # A local store: over `dummy://` a kept round is smaller, and half the runs keep none.
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp_path / "store")], capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, done.stderr[-2000:]
    used = [int(line) for line in done.stdout.split()]
    assert len(used) == 10, done.stdout
    # 4 MiB with the patch, 9 MiB or more for a round that keeps its evaluation.
    assert max(used) < 8, f"MiB in use after each closed round: {used}"
