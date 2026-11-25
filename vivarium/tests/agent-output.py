"""The agent channel carries large answers, one after another (nixidae/nixidae#1178).

Each command writes 18,760,001 bytes to stderr. A QEMU guest whose agent
was on a console port (hvc0) stopped answering at the first: QEMU drops
what a console port's socket cannot take at once.
"""

from __future__ import annotations

from vivarium_runner import Machines

COMMAND = "head -c 14000000 /dev/zero | base64 -w 200 >&2"
SIZE = 18_760_001
CALLS = 8


async def test(vms: Machines) -> None:
    for call in range(CALLS):
        done = await vms.one.succeed(COMMAND, timeout=60)
        if len(done.stderr) != SIZE:
            raise AssertionError(f"call {call}: {len(done.stderr)} bytes of stderr, not {SIZE}")
        print(f"[test] call {call}: {SIZE} bytes")
    print(f"[test] {(await vms.one.succeed('hostname')).stdout.strip()} still answers")
