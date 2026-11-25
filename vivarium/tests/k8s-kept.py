"""One run of a kept cluster: the first makes a ConfigMap, the next finds it.

The control plane keeps its cluster and the worker keeps nothing, so a
second run resumes on the control plane and joins the worker again in
place of its old Node. The first run loads an image; the second finds a
pod of it running from the control plane's kept containerd.
"""

from __future__ import annotations

import shutil

import anyio
from vivarium_k8s import CONTROL_PLANE_TAINT, bring_up, kubectl, load_images, until
from vivarium_runner import Machine, Machines

PROBE = "kept-probe"
IMAGE = "vivarium.test/loaded:1"
WAIT = 180


def pod(name: str) -> str:
    return f"""apiVersion: v1
kind: Pod
metadata: {{name: {name}}}
spec:
  nodeName: cp
  containers:
  - name: main
    image: {IMAGE}
    imagePullPolicy: Never
    command: [/bin/sh, -c, "sleep 100000"]
"""


async def state(cp: Machine, name: str, what: str, wanted: str) -> None:
    path = {
        "phase": "{.status.phase}",
        "waiting": "{.status.containerStatuses[0].state.waiting.reason}",
    }[what]

    async def check() -> tuple[bool, str]:
        found = (await kubectl(cp, f"get pod {name} --output jsonpath='{path}'")).strip()
        return found == wanted, found

    await until(f"pod {name} {what} {wanted}", check, WAIT, cp)


async def load(vms: Machines, cp: Machine) -> None:
    """Never pulled before the load, running after it."""
    await cp.succeed(f"kubectl apply --filename - <<'EOF'\n{pod('unloaded')}EOF")
    await state(cp, "unloaded", "waiting", "ErrImageNeverPull")
    await kubectl(cp, "delete pod unloaded --wait")
    # A copy outside the store, as `docker save` writes one: the /artifacts route.
    copy = vms.artifacts / "loaded.tar.gz"
    await anyio.to_thread.run_sync(shutil.copyfile, vms.settings["loaded"], copy)
    loaded = await load_images(vms, [copy])
    if sorted(loaded) != sorted(vms.settings["kubernetes"]["nodes"]) or any(
        images != [IMAGE] for images in loaded.values()
    ):
        raise RuntimeError(f"load_images reported {loaded}")
    await cp.succeed(f"kubectl apply --filename - <<'EOF'\n{pod('loaded')}EOF")
    await state(cp, "loaded", "phase", "Running")
    print(f"[k8s-kept] loaded {IMAGE} into {sorted(loaded)}; its pod runs", flush=True)


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
        await load(vms, cp)
    elif uid != made:
        raise RuntimeError(f"the kept cluster has {PROBE} {uid or '(none)'}, the last run made {made}")
    else:
        print(f"[k8s-kept] found {PROBE} {uid} from the last run", flush=True)
        await state(cp, "loaded", "phase", "Running")
        print(f"[k8s-kept] the pod of {IMAGE} runs again, from the kept containerd", flush=True)
    taints = await kubectl(cp, f"get node {cp.name} --output jsonpath={{.spec.taints[*].key}}")
    if CONTROL_PLANE_TAINT in taints.split():
        raise RuntimeError(f"{cp.name} keeps {CONTROL_PLANE_TAINT}")
    ready = await kubectl(cp, "get nodes --no-headers")
    if sum(" Ready " in f" {line} " for line in ready.splitlines()) != len(vms.settings["kubernetes"]["nodes"]):
        raise RuntimeError(f"not every node is Ready:\n{ready}")
