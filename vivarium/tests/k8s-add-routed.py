"""A worker added behind the router: the host reaches its pod through the
router, by the pod's address and by a LoadBalancer Service's.

The router routes a pod range to the node whose label
`vivarium.dev/uplink-address` names it. Without the label the pod's
address does not answer from the host. The Service's address answers
through any node, so it alone does not show the label works.
"""

from __future__ import annotations

import anyio
from vivarium_k8s import UPLINK_LABEL, add_node, bring_up, get_json, kubectl, remove_node, until, wait_for_pods
from vivarium_runner import Machines

WEB = """
apiVersion: v1
kind: Pod
metadata: {{name: web, labels: {{app: web}}}}
spec:
  nodeName: {node}
  containers:
  - name: web
    image: {image}
    imagePullPolicy: Never
    command: [/bin/sh, -c, "mkdir -p /www && echo {greeting} > /www/index.html && exec /bin/httpd -f -p 8080 -h /www"]
    volumeMounts: [{{name: nix-store, mountPath: /nix/store, readOnly: true}}]
  volumes: [{{name: nix-store, hostPath: {{path: /nix/store, type: Directory}}}}]
---
apiVersion: v1
kind: Service
metadata: {{name: web}}
spec:
  type: LoadBalancer
  selector: {{app: web}}
  ports: [{{port: 80, targetPort: 8080}}]
"""
GREETING = "hello-from-an-added-worker"
WAIT = 120


async def test(vms: Machines) -> None:
    cp = await bring_up(vms)
    worker = await add_node(vms, "worker", "w1")
    node = await get_json(cp, "get node w1")
    cidr = node["spec"]["podCIDR"]

    manifest = WEB.format(node="w1", image=vms.settings["workloadImage"], greeting=GREETING)
    await cp.succeed(f"cat <<'EOF' | kubectl apply --filename -\n{manifest}\nEOF", timeout=180)
    await wait_for_pods(cp, "--field-selector metadata.name=web", namespace="default")
    pod = await kubectl(cp, "get pod web --output jsonpath={.status.podIP}")

    async def given() -> tuple[bool, str]:
        out = (await kubectl(cp, "get service web --output jsonpath='{.status.loadBalancer.ingress[0].ip}'")).strip()
        return bool(out), out

    service = await until("the router's controller to give web an address", given, WAIT, cp)
    [proxy] = vms["router"].reachable(1080)
    for address, port in ((pod, 8080), (service, 80)):

        async def reach(url: str = f"http://{address}:{port}/") -> tuple[bool, str]:
            done = await anyio.run_process(
                [vms.settings["curl"], "--silent", "--show-error", "--max-time", "10", "--socks5", proxy, url],
                check=False,
            )
            out = (done.stdout or done.stderr).decode().strip()
            return done.returncode == 0 and GREETING in out, out

        await until(f"{address}:{port} from the host", reach, WAIT, cp)
        print(f"[k8s-add-routed] the host reached w1's pod at {address}:{port}", flush=True)

    label = node["metadata"].get("labels", {}).get(UPLINK_LABEL)
    if label != worker.spec.uplink_address:
        raise RuntimeError(f"w1's {UPLINK_LABEL} is {label}, not its lease {worker.spec.uplink_address}")
    print(f"[k8s-add-routed] w1 behind the router at {label}, pods {cidr}", flush=True)

    await remove_node(vms, "w1")
    router = vms["router"]

    async def unrouted() -> tuple[bool, str]:
        table = (await router.succeed("ip route show table all")).stdout
        return cidr not in table, table

    await until(f"the router to drop its route to {cidr}", unrouted, WAIT, cp)
    print("[k8s-add-routed] w1 removed; the router routes its pods no more", flush=True)
