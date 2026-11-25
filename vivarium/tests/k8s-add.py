"""Workers added to a running cluster from a template, and one removed again.

Each instance must register the address the runner gave it, not the
relay's address every guest shares: a wrong one can still go Ready. So
this checks the Node's InternalIP, `kubectl exec` into a pod on the new
node (the API server dials its kubelet there), and pods on every node
reaching each other.
"""

from __future__ import annotations

from vivarium_k8s import add_node, bring_up, get_json, kubectl, remove_node, until, wait_for_pods
from vivarium_runner import Machine, Machines

POD = """
apiVersion: v1
kind: Pod
metadata:
  name: {name}
spec:
  nodeName: {node}
  containers:
  - name: web
    image: {image}
    imagePullPolicy: Never
    command:
    - /bin/sh
    - -c
    - mkdir -p /www && echo {name} > /www/index.html && exec /bin/httpd -f -p 8080 -h /www
    volumeMounts:
    - name: nix-store
      mountPath: /nix/store
      readOnly: true
  volumes:
  - name: nix-store
    hostPath:
      path: /nix/store
      type: Directory
"""
WAIT = 120


async def internal_ip(cp: Machine, name: str) -> str:
    node = await get_json(cp, f"get node {name}")
    return str(next(a["address"] for a in node["status"]["addresses"] if a["type"] == "InternalIP"))


async def reaches(cp: Machine, source: str, target: str, address: str) -> None:
    """Pod *source* gets pod *target*'s page from *address*."""

    async def check() -> tuple[bool, str]:
        done = await cp.execute(f"kubectl exec {source} -- wget -T 5 -qO- http://{address}:8080/ 2>&1", timeout=60)
        return done.returncode == 0 and target in done.stdout, done.stdout.strip()

    await until(f"{source} to reach {target}", check, WAIT, cp)


async def test(vms: Machines) -> None:
    cp = await bring_up(vms)
    workers = [await add_node(vms, "worker", name) for name in ("w1", "w2")]

    for vm in workers:
        registered = await internal_ip(cp, vm.name)
        if registered != vm.ip:
            raise RuntimeError(f"{vm.name} registered {registered}, not its address {vm.ip}")
        # kubelet takes the last --node-ip. 0.0.0.0 makes it guess, and the
        # guess can land on the same address as above.
        argv = (await vm.succeed("tr '\\0' '\\n' < /proc/$(pidof kubelet)/cmdline")).stdout.split()
        given = [arg.removeprefix("--node-ip=") for arg in argv if arg.startswith("--node-ip=")]
        if given[-1:] != [vm.ip]:
            raise RuntimeError(f"{vm.name}'s kubelet runs with --node-ip {given}, not {vm.ip}")
    print(f"[k8s-add] {', '.join(f'{vm.name} on {vm.ip}' for vm in workers)}", flush=True)

    image = vms.settings["workloadImage"]
    names = [cp.name, *(vm.name for vm in workers)]
    for node in names:
        manifest = POD.format(name=f"on-{node}", node=node, image=image)
        await cp.succeed(f"cat <<'EOF' | kubectl apply --filename -\n{manifest}\nEOF", timeout=180)
    for node in names:
        await wait_for_pods(cp, f"--field-selector metadata.name=on-{node}", namespace="default")
    addresses = {node: await kubectl(cp, f"get pod on-{node} --output jsonpath={{.status.podIP}}") for node in names}

    for vm in workers:
        await kubectl(cp, f"exec on-{vm.name} -- true")
    for source in names:
        for target in names:
            if source != target:
                await reaches(cp, f"on-{source}", f"on-{target}", addresses[target])
    print(f"[k8s-add] pods on {names} reach each other", flush=True)

    await remove_node(vms, "w1")
    if "w1" in vms or "w1" in (await kubectl(cp, "get nodes --output name")):
        raise RuntimeError("w1 is still there after remove_node")
    await reaches(cp, f"on-{cp.name}", "on-w2", addresses["w2"])
    print("[k8s-add] w1 removed; w2 still answers", flush=True)
