# A Kubernetes developer environment

One kubeadm node runs your app. You reach the app from your own shell
and browser by its Service name, over HTTPS:

- `https://hello.default.svc.cluster.local/`: the app's Service. Its
  `https` port is 8443, and you still type no port.

You need no port forward, no `kubectl port-forward` and no change to your
trust store. The cluster stays across `vivarium down` and `vivarium up`.
`checks.k8s-devenv` runs this example in the Nix sandbox and proves each
command below from the host. `checks.k8s-devenv-kept` runs it twice into
one environment and proves that the second run resumes the cluster.

## The parts

| File | What it gives |
| --- | --- |
| `default.nix` | The node, and the recipe: `loadBalancer` (Service names and LoadBalancer addresses through the router's proxy) and `certManager` (a ClusterIssuer `vivarium` on the run's lab CA). `persist` keeps the cluster. The app's image, built by Nix, goes in `extraImages`. |
| `app/server.py` | The app: one page over HTTPS on 8443. |
| `app/hello.yaml` | A `Certificate`, the `Deployment`, and a LoadBalancer `Service` whose port is named `https`. |
| `phases/cluster.py` | `bring_up`: kubeadm, the LoadBalancer controller, cert-manager, `access/kubeconfig`. |
| `phases/deploy.py` | `kubectl apply` from the host, as you would. |
| `phases/check.py` | The commands below, from the host. |

The Service's port is named `https`, so Kubernetes publishes the SRV
record `_https._tcp.hello.default.svc.cluster.local`. The router's proxy
follows it to port 8443. The node imports the image at boot. There is no
registry, so the Deployment says `imagePullPolicy: Never`.

## Use it

From the `vivarium` directory of nixidae:

```console
$ vivarium up checks.k8s-devenv
$ vivarium shell k8s-devenv
$ kubectl get pods
$ curl https://hello.default.svc.cluster.local/
hello from the cluster
$ exit
$ vivarium browser k8s-devenv --url hello.default.svc.cluster.local
$ vivarium down k8s-devenv
```

`vivarium shell` sets `KUBECONFIG`, the proxy variables and
`SSL_CERT_FILE`. `vivarium browser` starts your default browser with a
profile of its own that uses the proxy and trusts the lab root.

## Change the app

Edit `app/server.py`, build the image, and load it into the live node:

```console
$ vivarium load k8s-devenv "$(nix build --no-link --print-out-paths --file . checks.k8s-devenv.image)"
$ vivarium shell k8s-devenv -- kubectl rollout restart deployment/hello
```

The image keeps its tag, so only pods that start after the load run it.
`vivarium up` again also brings the new image, from `extraImages`.

## Limits

- Only programs that use the proxy reach the cluster: curl, kubectl, Go
  and Python programs, browsers.
- Names follow SRV only for ports 443 and 80. Another port is part of the
  URL: `https://hello.default.svc.cluster.local:8443/`.
- The lab CA signs only for `cluster.local`, `test`, `internal`,
  `localhost` and private addresses. A `Certificate` for a public name
  fails verification everywhere.
