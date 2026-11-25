"""checks.k8s-kept: two runs of one Kubernetes environment.

The first makes the cluster; the second must resume it (no `kubeadm
init`), find the first run's ConfigMap, and join the worker that kept
nothing in place of its old Node.

Usage: k8s-kept.py DRIVER TMPDIR
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from harness import Driven

RESUMED = "[k8s] cp: resumes the kept cluster"


def main() -> None:
    driver, tmp = sys.argv[1], Path(sys.argv[2])
    # Where `places` looks for environments: this check's own.
    os.environ["VIVARIUM_STATE"] = str(tmp / "state")
    environment = tmp / "state" / "envs" / "k8s-kept"
    environment.parent.mkdir(parents=True)
    for run in (1, 2):
        driven = Driven(driver, environment, "--no-control")
        driven.wait(f"in {environment}")
        driven.want({"up": "passed"})
        log = (environment / "log").read_text()
        if (RESUMED in log) != (run == 2):
            sys.exit(f"run {run}: {'no' if run == 2 else 'a'} resume of the kept cluster")
        kept = json.loads((environment / "state.json").read_text())
        if "probe" not in kept["keys"] or kept["outcomes"].get("up") != "passed":
            sys.exit(f"after run {run}, state.json is {kept}")
        print(f"ok: run {run} {'resumed' if run == 2 else 'made'} the cluster")


if __name__ == "__main__":
    main()
