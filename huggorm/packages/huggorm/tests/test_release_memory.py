"""`release_memory` gives the freed heap back to the kernel.

The collector keeps a freed block mapped until it has stayed free through
several collections, so an idle process holds its peak: an SBOM worker
kept 13.6 GiB after an evaluation it had freed (nixidae/nixidae#1624).

In a child: the measure is process-wide.
"""

import pathlib
import subprocess
import sys

import pytest
from nixversion import needs_collector

CHILD = """
import gc
import pathlib
import sys
import anyio
from huggorm import AsyncSession
from huggorm_bindings import collect_garbage, gc_stats, release_memory

EXPR = '''
let
  item = i: { n = i; s = "x${toString i}"; l = builtins.genList (j: j * i) 20; };
in
builtins.length (builtins.filter (x: x.n >= 0 && builtins.length x.l > 0 && x.s != "") (builtins.genList item 400000))
'''

def rss_mib():
    status = pathlib.Path("/proc/self/status").read_text()
    return int(next(l.split()[1] for l in status.splitlines() if l.startswith("VmRSS:"))) // 1024

async def main():
    async with AsyncSession(sys.argv[1]) as session:
        state = session.eval(session.store())
        await (await state.eval_expr(EXPR, "/")).to_json()
    gc.collect()
    await anyio.to_thread.run_sync(collect_garbage)
    before = (rss_mib(), gc_stats()["unmapped_bytes"] >> 20)
    await anyio.to_thread.run_sync(release_memory)
    after = (rss_mib(), gc_stats()["unmapped_bytes"] >> 20)
    print(*before, *after)

anyio.run(main)
"""


@pytest.mark.skipif(sys.platform != "linux", reason="measured on Linux only")
@needs_collector
def test_release_memory_unmaps_the_freed_heap(tmp_path: pathlib.Path) -> None:
    done = subprocess.run(
        [sys.executable, "-c", CHILD, str(tmp_path / "store")], capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, done.stderr[-2000:]
    rss_before, unmapped_before, rss_after, unmapped_after = map(int, done.stdout.split())
    assert unmapped_after - unmapped_before >= 64, done.stdout
    assert rss_before - rss_after >= 64, done.stdout
