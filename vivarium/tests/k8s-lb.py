"""A LoadBalancer Service, reached from the host through the router's
SOCKS5 proxy: by the address the router's controller gave it, and by the
Service's cluster name. The host's kubectl reaches the API by its cluster
name through the proxy too, and another guest reaches the Service's
address through the router.

curl runs on the host, in a process of its own, with `--socks5-hostname`:
the proxy resolves the name in the lab, as a browser or kubectl would ask it
to.
"""

from __future__ import annotations

import ipaddress
import json

import anyio
from vivarium_k8s import bring_up, kubectl, until, wait_for_pods
from vivarium_runner import Machines

WEB = """
apiVersion: v1
kind: Pod
metadata: {{name: web, labels: {{app: web}}}}
spec:
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

GREETING = "hello-through-the-router"
POOL = ipaddress.IPv4Network("10.200.0.0/24")


async def curl(vms: Machines, proxy: str, url: str) -> tuple[bool, str]:
    done = await anyio.run_process(
        [vms.settings["curl"], "--silent", "--show-error", "--max-time", "10", "--socks5-hostname", proxy, url],
        check=False,
    )
    return done.returncode == 0, (done.stdout or done.stderr).decode().strip()


async def test(vms: Machines) -> None:
    cp = await bring_up(vms)
    [proxy] = vms["router"].reachable(1080)
    manifest = WEB.format(image=vms.settings["workloadImage"], greeting=GREETING)
    await cp.succeed(f"cat <<'EOF' | kubectl apply --filename -\n{manifest}\nEOF", timeout=180)
    await wait_for_pods(cp, "--field-selector metadata.name=web", namespace="default")

    async def address() -> tuple[bool, str]:
        out = (await kubectl(cp, "get service web --output jsonpath='{.status.loadBalancer.ingress[0].ip}'")).strip()
        return bool(out), out

    given = await until("the router's controller to give web an address", address, 120, cp)
    if ipaddress.IPv4Address(given) not in POOL:
        raise RuntimeError(f"web got {given}, outside the pool {POOL}")

    for url in (f"http://{given}/", "http://web.default.svc.cluster.local/"):

        async def reach(url: str = url) -> tuple[bool, str]:
            ok, out = await curl(vms, proxy, url)
            return ok and GREETING in out, out

        body = await until(f"{url} through the proxy", reach, 120, cp)
        print(f"[k8s-lb] the host reached {url} through {proxy}: {body}", flush=True)

    async def from_client() -> tuple[bool, str]:
        done = await vms["client"].execute(f"curl --silent --show-error --max-time 10 http://{given}/")
        return done.returncode == 0 and GREETING in done.stdout, done.stdout + done.stderr

    body = await until(f"{given} from the guest client", from_client, 120, cp)
    print(f"[k8s-lb] client reached {given} through the router: {body.strip()}", flush=True)

    # The API by its cluster name, the name its certificate holds: no
    # forward of 6443 and no `tls-server-name`.
    config = json.loads(await anyio.Path(vms.access / "kubeconfig").read_text())
    for cluster in config["clusters"]:
        cluster["cluster"]["server"] = "https://kubernetes.default.svc.cluster.local"
        cluster["cluster"].pop("tls-server-name", None)
        cluster["cluster"]["proxy-url"] = f"socks5://{proxy}"
    proxied = vms.access / "kubeconfig-socks"
    await anyio.Path(proxied).write_text(json.dumps(config))
    done = await anyio.run_process(
        [vms.settings["kubectl"], "--kubeconfig", str(proxied), "get", "service", "web", "--output", "wide"],
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"kubectl through the proxy failed: {done.stderr.decode()}")
    print("[k8s-lb] the host's kubectl through the proxy:\n" + done.stdout.decode(), flush=True)
