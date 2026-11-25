"""Three control planes; the API endpoint outlives one API server.

The router balances the endpoint over the control planes' API servers.
This stops the API server of the control plane that ran `kubeadm init`
and asks the endpoint again, from another control plane and from the
host, several times, so that the balancer picks each control plane.
"""

from __future__ import annotations

import anyio
from vivarium_k8s import CONTROL_PLANE, bring_up, get_json, kubectl, nodes, until
from vivarium_runner import Machine, Machines

MANIFEST = "/etc/kubernetes/manifests/kube-apiserver.yaml"
ASKS = 9
WAIT = 120


async def own_api(vm: Machine, on: Machine) -> tuple[bool, str]:
    """Whether *on* gets an answer from *vm*'s own API server, not the endpoint's."""
    done = await on.execute(f"kubectl --server https://{vm.ip}:6443 get --raw /readyz 2>&1")
    return done.returncode == 0, done.stdout.strip()


async def test(vms: Machines) -> None:
    cp = await bring_up(vms)
    control_planes = sorted(name for name, node in nodes(vms).items() if node["role"] == CONTROL_PLANE)
    others = [vms[name] for name in control_planes if name != cp.name]

    members = await get_json(cp, "get pods --namespace kube-system --selector component=etcd")
    hosts = sorted(pod["spec"]["nodeName"] for pod in members["items"])
    if hosts != control_planes:
        raise RuntimeError(f"etcd runs on {hosts}, not on each control plane")
    print(f"[k8s-ha] etcd on {hosts}", flush=True)

    # The kubelet stops a static pod whose manifest leaves the directory.
    await cp.succeed(f"mv {MANIFEST} /root/")

    async def stopped() -> tuple[bool, str]:
        up, said = await own_api(cp, others[0])
        return not up, said

    await until(f"{cp.name}'s API server to stop", stopped, WAIT, cp)
    print(f"[k8s-ha] {cp.name}'s API server is stopped", flush=True)

    asker = others[0]
    for ask in range(ASKS):
        await kubectl(asker, "get nodes")
        await kubectl(asker, f"create configmap ha-{ask} --from-literal=ask={ask}")
    print(f"[k8s-ha] {asker.name}: {ASKS} reads and writes through the endpoint", flush=True)

    kubeconfig = str(vms.access / "kubeconfig")
    for _ in range(ASKS):
        done = await anyio.run_process(
            [vms.settings["kubectl"], "--kubeconfig", kubeconfig, "get", "configmap", f"ha-{ASKS - 1}"], check=False
        )
        if done.returncode != 0:
            raise RuntimeError(f"the host's kubectl through the router failed: {done.stderr.decode()}")
    print(f"[k8s-ha] the host: {ASKS} reads through the router's forward", flush=True)
