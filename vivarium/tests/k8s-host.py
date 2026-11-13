"""A one-node cluster, reached from the host through `<out>/access/kubeconfig`.

`kubectl` runs on the host, in a process of its own, so the request
crosses the forward of 6443 and the certificate check against
`tls-server-name` as a person's `kubectl` would.
"""

import json

import anyio
from vivarium_k8s import bring_up
from vivarium_runner import Machines


async def test(vms: Machines) -> None:
    await bring_up(vms)
    kubeconfig = vms.access / "kubeconfig"
    result = await anyio.run_process(
        [vms.settings["kubectl"], "--kubeconfig", str(kubeconfig), "get", "nodes", "--output", "json"],
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"kubectl on the host failed (exit {result.returncode}): {result.stderr.decode()}")
    names = [item["metadata"]["name"] for item in json.loads(result.stdout)["items"]]
    if names != ["cp"]:
        raise RuntimeError(f"the host's kubectl listed {names}, not the one node cp")
    print(f"[k8s-host] the host's kubectl listed {names} through {kubeconfig}", flush=True)
