"""checks.k8s-devenv-kept: two runs of examples/k8s-devenv into one environment.

The first makes the cluster; the second must resume it (no `kubeadm
init`) and pass every phase again, HTTPS from the host included.

Usage: k8s-devenv-kept.py DRIVER TMPDIR
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from harness import Driven

RESUMED = "[k8s] cp: resumes the kept cluster"
PHASES = {"cluster": "passed", "deploy": "passed", "check": "passed"}


def main() -> None:
    driver, tmp = sys.argv[1], Path(sys.argv[2])
    os.environ["VIVARIUM_STATE"] = str(tmp / "state")
    environment = tmp / "state" / "envs" / "k8s-devenv"
    environment.parent.mkdir(parents=True)
    for run in (1, 2):
        driven = Driven(driver, environment, "--no-control")
        driven.wait(f"in {environment}")
        driven.want(PHASES)
        if (RESUMED in (environment / "log").read_text()) != (run == 2):
            driven.fail(f"run {run}: {'no' if run == 2 else 'a'} resume of the kept cluster")
        print(f"ok: run {run} {'resumed' if run == 2 else 'made'} the cluster and passed every phase")


if __name__ == "__main__":
    main()
