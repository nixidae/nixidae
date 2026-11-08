#!/usr/bin/env python3
"""A pytest suite inside one guest: the `suites` recipe's phase.

One script serves every suite, told apart by `vms.phase`;
`settings.guestSuites.<phase>` says what each one runs. The suite writes
JUnit to `/artifacts/junit/<phase>.xml`, which the session reads back as
cases when the phase ends, and its output to `/artifacts/<phase>.log`:
thousands of lines do not belong on the agent's channel.
"""

from __future__ import annotations

import shlex

from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    name = vms.phase
    suites = vms.settings["guestSuites"]
    if name is None or name not in suites:
        raise RuntimeError(f"no suite for phase {name!r}; settings.guestSuites has {sorted(suites)}")
    suite = suites[name]
    vm = vms[suite["node"]]
    words = [
        suite["pytest"],
        "-p",
        "no:cacheprovider",
        *suite["args"],
        f"--junitxml=/artifacts/junit/{name}.xml",
        suite["tests"],
        *vms.pytest_args,
    ]
    # `environment` values are shell words on purpose: `$(command -v nix)`.
    env = "".join(f"{key}={value} " for key, value in sorted(suite["environment"].items()))
    command = f"cd {shlex.quote(suite['directory'])} && {env}{shlex.join(words)} > /artifacts/{name}.log 2>&1"
    user = suite["user"]
    await vm.succeed("mkdir -p /artifacts/junit")
    prefix = ""
    if suite["delegateCgroup"]:
        # The agent's cgroup is root's. A suite that makes cgroups of its
        # own needs one it owns, as `Delegate=yes` gives a unit. `su` does
        # not move a process, so the shell moves itself first.
        cgroup = f"/sys/fs/cgroup/vivarium-suite-{name}"
        await vm.succeed(
            "echo '+cpu +memory +io' > /sys/fs/cgroup/cgroup.subtree_control && "
            f"mkdir -p {cgroup} && "
            f"chown {user} {cgroup} {cgroup}/cgroup.procs {cgroup}/cgroup.threads {cgroup}/cgroup.subtree_control"
        )
        prefix = f"echo $$ > {cgroup}/cgroup.procs && "
    # `su -`, root too: a login shell gives the user its own HOME, where
    # pytest writes. The agent's environment is not a login's.
    run = f"su - {shlex.quote(user)} -c {shlex.quote(command)}"
    rc, _ = await vm.execute(f"{prefix}{run}", timeout=suite["timeout"], label=f"pytest {name}")
    tail = (await vm.succeed(f"tail -n 3 /artifacts/{name}.log")).strip()
    print(f"[suite] {name}: exit {rc}\n[suite]   {tail}")
    if rc != 0:
        last = tail.splitlines()[-1] if tail else ""
        raise AssertionError(f"{suite['tests']} exited {rc}: {last}")
