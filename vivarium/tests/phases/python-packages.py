#!/usr/bin/env python3
"""A phase imports what the run and its guests put in `python.packages`.

`tomli_w` comes from the run's option, `humanize` from the guest's. Both
are typed, so the type check resolves these imports from the same venv
that runs the phase.
"""

import humanize
import tomli_w

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    assert tomli_w.dumps({"a": 1}) == "a = 1\n"
    assert humanize.naturalsize(1000) == "1.0 kB"
    out = await vms.one.succeed("echo up")
    assert out.strip() == "up"
