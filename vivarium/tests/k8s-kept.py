"""One run of a kept cluster: the first makes a ConfigMap, the next finds it.

The control plane keeps its cluster and the worker keeps nothing, so a
second run resumes on the control plane and joins the worker again in
place of its old Node.
"""

from __future__ import annotations

from vivarium_k8s import CONTROL_PLANE_TAINT, bring_up, kubectl
from vivarium_runner import Machines

PROBE = "kept-probe"


async def test(vms: Machines) -> None:
    cp = await bring_up(vms, schedulable=True)
    uid = (await kubectl(cp, f"get configmap {PROBE} --ignore-not-found --output jsonpath={{.metadata.uid}}")).strip()
    made = vms.state.get("probe")
    if made is None:
        if uid:
            raise RuntimeError(f"a blank cluster already has {PROBE} ({uid})")
        await kubectl(cp, f"create configmap {PROBE} --from-literal=run=1")
        uid = (await kubectl(cp, f"get configmap {PROBE} --output jsonpath={{.metadata.uid}}")).strip()
        vms.state["probe"] = uid
        print(f"[k8s-kept] made {PROBE} {uid}", flush=True)
    elif uid != made:
        raise RuntimeError(f"the kept cluster has {PROBE} {uid or '(none)'}, the last run made {made}")
    else:
        print(f"[k8s-kept] found {PROBE} {uid} from the last run", flush=True)
    taints = await kubectl(cp, f"get node {cp.name} --output jsonpath={{.spec.taints[*].key}}")
    if CONTROL_PLANE_TAINT in taints.split():
        raise RuntimeError(f"{cp.name} keeps {CONTROL_PLANE_TAINT}")
    ready = await kubectl(cp, "get nodes --no-headers")
    if sum(" Ready " in f" {line} " for line in ready.splitlines()) != len(vms.settings["kubernetes"]["nodes"]):
        raise RuntimeError(f"not every node is Ready:\n{ready}")
