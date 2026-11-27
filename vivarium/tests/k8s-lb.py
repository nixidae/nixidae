"""A LoadBalancer Service, reached from the host through the router's
SOCKS5 proxy: by the address the router's controller gave it, and by the
Service's cluster name. The host's kubectl reaches the API by its cluster
name through the proxy too, and another guest reaches the Service's
address through the router.

HTTPS: cert-manager issues a certificate from the run's intermediate CA,
and the host trusts it through `access/ca-bundle.pem`, and only through
it. A certificate for a public name, from the same CA, fails verification.

curl runs on the host, in a process of its own, with `--socks5-hostname`:
the proxy resolves the name in the lab, as a browser or kubectl would ask it
to.
"""

from __future__ import annotations

import ipaddress
import json

import anyio
from vivarium_k8s import bring_up, kubectl, until, wait_for_pods
from vivarium_runner import Machine, Machines

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

TLS = """
apiVersion: cert-manager.io/v1
kind: Certificate
metadata: {{name: {name}}}
spec:
  secretName: {name}
  dnsNames: [{dns}]
  issuerRef: {{kind: ClusterIssuer, name: vivarium}}
"""

SERVER = """
apiVersion: v1
kind: Pod
metadata: {{name: web-tls, labels: {{app: web-tls}}}}
spec:
  containers:
  - name: lab
    image: {image}
    imagePullPolicy: Never
    command: [{openssl}, s_server, -accept, "8443", -www, -cert, /lab/tls.crt, -key, /lab/tls.key, -cert_chain, /lab/tls.crt]
    volumeMounts:
    - {{name: nix-store, mountPath: /nix/store, readOnly: true}}
    - {{name: lab, mountPath: /lab, readOnly: true}}
  - name: public
    image: {image}
    imagePullPolicy: Never
    command: [{openssl}, s_server, -accept, "8444", -www, -cert, /public/tls.crt, -key, /public/tls.key, -cert_chain, /public/tls.crt]
    volumeMounts:
    - {{name: nix-store, mountPath: /nix/store, readOnly: true}}
    - {{name: public, mountPath: /public, readOnly: true}}
  volumes:
  - {{name: nix-store, hostPath: {{path: /nix/store, type: Directory}}}}
  - {{name: lab, secret: {{secretName: web-tls}}}}
  - {{name: public, secret: {{secretName: public-tls}}}}
---
apiVersion: v1
kind: Service
metadata: {{name: web-tls}}
spec:
  selector: {{app: web-tls}}
  ports:
  - {{name: lab, port: 443, targetPort: 8443}}
  - {{name: public, port: 8444, targetPort: 8444}}
---
apiVersion: v1
kind: Service
metadata: {{name: web-alt}}
spec:
  selector: {{app: web-tls}}
  ports: [{{name: https, port: 8443, targetPort: 8443}}]
---
apiVersion: v1
kind: Service
metadata: {{name: web-other}}
spec:
  selector: {{app: web-tls}}
  ports: [{{name: tls, port: 8443, targetPort: 8443}}]
"""

LAB_NAMES = ", ".join(f"{name}.default.svc.cluster.local" for name in ("web-tls", "web-alt", "web-other"))

GREETING = "hello-through-the-router"
POOL = ipaddress.IPv4Network("10.200.0.0/24")


async def curl(vms: Machines, proxy: str, url: str, *extra: str) -> tuple[bool, str]:
    done = await anyio.run_process(
        [vms.settings["curl"], "--silent", "--show-error", "--max-time", "10", "--socks5-hostname", proxy, *extra, url],
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
    await https(vms, cp, proxy)


async def https(vms: Machines, cp: Machine, proxy: str) -> None:
    for name, dns in (("web-tls", LAB_NAMES), ("public-tls", "example.com")):
        await cp.succeed(f"cat <<'EOF' | kubectl apply --filename -\n{TLS.format(name=name, dns=dns)}\nEOF")
        await cp.succeed(f"kubectl wait --for=condition=Ready certificate/{name} --timeout=120s", timeout=130)
    server = SERVER.format(image=vms.settings["workloadImage"], openssl=vms.settings["openssl"])
    await cp.succeed(f"cat <<'EOF' | kubectl apply --filename -\n{server}\nEOF")
    await wait_for_pods(cp, "--field-selector metadata.name=web-tls", namespace="default")

    bundle = str(vms.access / "ca-bundle.pem")
    url = "https://web-tls.default.svc.cluster.local/"

    async def trusted() -> tuple[bool, str]:
        ok, out = await curl(vms, proxy, url, "--cacert", bundle)
        return ok and "s_server" in out, out

    await until(f"{url} with the bundle", trusted, 120, cp)
    print(f"[k8s-lb] the host trusts {url} through access/ca-bundle.pem", flush=True)

    # web-alt has no port 443: only its `https` port's SRV record, from
    # CoreDNS, sends the proxy to 8443. web-other's 8443 port has another
    # name, so it has no `_https._tcp` record and gets no page.
    moved = "https://web-alt.default.svc.cluster.local/"

    async def by_service() -> tuple[bool, str]:
        ok, out = await curl(vms, proxy, moved, "--cacert", bundle)
        return ok and "s_server" in out, out

    await until(f"{moved} through its SRV record", by_service, 60, cp)
    print(f"[k8s-lb] the proxy follows {moved} to its https port, 8443", flush=True)
    ok, out = await curl(vms, proxy, "https://web-other.default.svc.cluster.local/", "--cacert", bundle)
    if ok:
        raise RuntimeError(f"web-other, whose port is not named https, answered on 443: {out}")
    print(f"[k8s-lb] web-other, port named tls: {out}", flush=True)

    # The same server against the public roots alone: refused for want of
    # an issuer (curl's 60), not for want of a trust file.
    ok, out = await curl(vms, proxy, url, "--cacert", vms.settings["publicRoots"])
    if ok or "(60)" not in out:
        raise RuntimeError(f"{url} against the public roots: {out}")
    print(f"[k8s-lb] with the public roots alone: {out.splitlines()[0]}", flush=True)

    # A certificate for a public name from the run's CA: the name
    # constraints refuse it, though the chain is the trusted one.
    ok, out = await curl(
        vms,
        proxy,
        "https://example.com:8444/",
        "--cacert",
        bundle,
        "--connect-to",
        "example.com:8444:web-tls.default.svc.cluster.local:8444",
    )
    if ok:
        raise RuntimeError(f"a certificate for example.com verified: {out}")
    print(f"[k8s-lb] example.com from the lab CA: {out}", flush=True)

    # The public roots survived the merge: the bundle holds more than the lab's.
    count = (await anyio.Path(bundle).read_text()).count("BEGIN CERTIFICATE")
    if count < 2:
        raise RuntimeError(f"access/ca-bundle.pem holds {count} certificate, so no public root")
