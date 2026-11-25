# vivarium howto

Each section is one task, with the commands for it. The examples use a
test named `mine` in `default.nix`. Replace the name with your test's.

[reference.md](reference.md) explains how a run behaves and why. This
file only tells you what to type.

## Run a test

```console
$ nix build --file . mine                          # in the sandbox, as CI runs it
$ nix run --file . mine.driver -- --out ./out      # by hand, output in ./out
$ nix run --file . mine.driverDebug -- --out ./out # by hand, pause at the first failure
```

A sandboxed verdict is a store path. For the same inputs, Nix reuses it
from the store or from a cache, whether the test passed or failed. To
boot the guests again, rebuild the attempt and keep the new run:

```console
$ nix build --rebuild --keep-failed --file . mine.attempt
```

Nix then says the output differs, because the timings always do, and
exits 1. The new run is at the attempt's path with `.check` added, and
its `status` holds the new verdict.

A run by hand prints the commands that reach it, before the first
phase and again at each pause:

```
[vivarium] wait for a pause or the verdict (exits 4 at a pause): /nix/store/…/bin/vivarium-runner monitor /home/me/out --quiet
[vivarium] run Python against the guests: /nix/store/…/bin/vivarium-runner ctl --out /home/me/out exec 'print(list(vms))'
[vivarium] run a file's `async def test(vms)`: /nix/store/…/bin/vivarium-runner ctl --out /home/me/out inject ./check.py
[vivarium] each phase's state: /nix/store/…/bin/vivarium-runner ctl --out /home/me/out state
[vivarium] resume a paused run: /nix/store/…/bin/vivarium-runner ctl --out /home/me/out continue
[vivarium] end the run; at ready, with its verdict: /nix/store/…/bin/vivarium-runner ctl --out /home/me/out stop
```

A run the `vivarium` CLI started also prints a `reload` line and a
`switch` line for its first guest, and names `vivarium` in place of
`vivarium-runner`.

Copy a line as it is. The paths are absolute, so the command works from
any directory and needs nothing on `PATH`. `<out>/run.json` holds the same
lines. In the rest of this file, `vivarium` is the CLI in
`packages.vivarium`, on `PATH` in the dev shell of each project that runs
vivarium tests. Without such a shell, `nix run --file . packages.vivarium --`
in a checkout of vivarium runs it. It is built on huggorm and takes `ctl`
and `monitor` as the runner does. The package also holds `vivarium-mcp` and an agent
skill, `share/skills/vivarium/vivarium`.

## Run a test on macOS

The reference's "Where each backend and uplink runs" says what runs on
macOS and on Linux, by hand and in a build.

A Mac runs krun guests behind a router guest: on macOS, a run that
leaves `router.enable` null gets one. A run that sets `router.enable = false` gives each
guest a vsock uplink of its own instead. The guests are Linux on the
Mac's processor, so a Linux builder for that system must be in
`/etc/nix/machines`.

```console
$ nix run --file . mine.krun.driver -- --out ./out
```

Every guest's forwards are on 127.0.0.1, which they share: a port that
is taken moves to the next free one, and the run says where. A run
killed with SIGKILL leaves its guests up (nixidae/nixidae#1415).

## Wait for the run without watching it

```console
$ nix run --file . mine.driverDebug -- --out ./out > run.log 2>&1 &
$ vivarium monitor ./out --quiet
```

Start the monitor together with the run. It waits for the run to start,
for up to 30 minutes (`--wait SECONDS`), because the driver first
evaluates and builds the test.

The monitor prints one line for each pause, failure and verdict. Then
it exits. `vivarium`, the driver, `ctl` and `monitor` use one table of
exit statuses:

| Exit | Meaning |
| --- | --- |
| 0 | The run passed, or `ctl` did what it asked. |
| 1 | The run failed, or what `ctl` asked for ran and failed: an `exec` that raised, a `pytest` with a failed case. |
| 2 | The run stopped without a verdict: it crashed, or the evaluation, build or spec failed. A command line the program refuses also exits 2. |
| 3 | No run: none started within `--wait`, none listens on the socket, or the stream ended before a verdict. |
| 4 | The run is paused. |
| 5 | `ctl stop` stopped the run. |
| 6 | An environment (`up`, `--hold`) is ready. |
| 7 | The run refused the request: the wrong time for it, or a phase, guest or segment it does not have. |

Start it in the background. When it exits, look at the exit code. After
you resume a paused run, start the monitor again. It does not stop again
for a pause that already ended.

With `--through-pauses`, the monitor prints a pause and runs on until
the verdict. Without `--quiet`, it also prints each phase that starts or
passes.

## Look inside a paused run

A paused run keeps its guests up, in the state the failure left.

```console
$ vivarium ctl --out ./out state
$ vivarium ctl --out ./out exec 'print((await one.succeed("systemctl --failed")).stdout)'
$ vivarium ctl --out ./out exec - < snippet.py
```

`exec` runs Python with top-level `await`. `vms` holds every guest, and
each guest is also a name of its own. A name you set stays for the next
`exec`. `exec` and `inject` also work while phases run.

## Fix a failing test without a new run

Do not edit a phase and start a new run. That evaluates and boots again.
Change the test file and send it to the guests that are already up:

```console
$ vivarium ctl --out ./out inject ./tests/check.py         # a file's `async def test(vms)`
$ vivarium ctl --out ./out pytest ./tests/guest -- -k etcd # pytest tests
$ vivarium ctl --out ./out run check                       # a phase from the test
```

`inject` and `pytest` read the file each time, so edit and send again.
A helper module the file imports stays the copy the run imported. To
read an edited helper again, name its directory: `--reload ./lib`.

To change the phases themselves, edit the test's Nix or its scripts and
reload a run you started by attribute:

```console
$ vivarium reload --out ./out             # evaluate again; the run takes the new phases
$ vivarium ctl --out ./out run check      # the new `check`, against the same guests
```

`reload` evaluates the session again and builds it, with the phases'
type check. The paused run then takes the new phase list: scripts,
pytest specs, `after`, `nodes`, settings and knobs. A phase that ran
keeps its verdict until it runs again, and a new phase runs when the
run resumes. The run refuses the reload when a guest, a template or a
segment changed: a guest needs a new run, or `switch` (below). The run must be paused. Like
`switch`, `reload` reads the attribute from `<out>/run.json`, and the
MCP server has a `reload(run)` tool.

When the test passes, resume:

```console
$ vivarium ctl --out ./out continue
```

`continue` returns when the run is no longer paused. With `--wait`, it
returns at the next pause or at the verdict instead, and exits as
`vivarium monitor` does (the table in "Wait for the run without
watching it").

`vivarium ctl --out ./out watch` prints the run's progress, pauses,
failures and verdict, one line each, until the run ends. Every run with
a control socket serves this stream; `vivarium monitor` reads it too.

`state` says why the run paused and, for each failed phase, the first
line of its error. `vivarium ctl --out ./out --json state` gives the
whole reply as JSON, with each error in full.

To end a run without the phases that are left, stop it:

```console
$ vivarium ctl --out ./out stop
```

`stop` works paused or running. It returns when the run has exited, after
the guests are down. The run exits 5, and its verdict says `stopped`, not
`failed`.

## Switch a guest to another configuration

Declare each configuration on the node. Each one is the node plus a
module:

```nix
nodes.one.vivarium.configurations.two = {
  services.nginx.enable = true;
};
```

A phase switches the running guest, as `nixos-rebuild` does:

```python
await vms.one.switch_to("two")  # switch: profile, then activate
await vms.one.switch_to("two", "test")  # activate only
await vms.one.switch_to()  # back to the booted system
done = await vms.one.switch_to("two", check=False)  # done.returncode 4: a unit failed
```

Host Nix evaluates each configuration. The guest has no nixpkgs. A
reboot does not boot the new system: the guest always boots the system
in its spec.

## Change a running guest's configuration

Write the change as a module file and apply it to a run you started by
attribute:

```console
$ vivarium run checks.switch --out ./out --break switch &
$ vivarium switch --out ./out --node one --module ./more.nix
$ vivarium switch --out ./out --node one --module ./more.nix --action test
```

`switch` reads the attribute and the file from `<out>/run.json`. A run
started this way prints its own `switch` line at each pause, and
`run.json` holds it. Copy it and change `--node` and `--module`. A run
started by `nix run` prints none: only the `vivarium` CLI can evaluate
it again. To evaluate such a run's session anyway, name the attribute:
`vivarium switch checks.switch --file . --out ./out ...`.

The MCP server's `switch` tool does the same: `switch(run, node, module)`.

The host evaluates the session again with the module on the node, and
builds the system. The guest gets the new closure in its store view and
its Nix database, then switches. Edit the file and apply it again to
iterate. Each apply is the booted configuration plus the file, so a
unit you remove from the file is stopped.

The evaluation reads the working tree as it is now. An edit to the
session's own files reaches the guest too.

## Keep guests up as an environment

Any test can be an environment: its phases set it up, and then the
guests stay up until you stop them. A Kubernetes cluster to work
against:

```console
$ vivarium up checks.k8s-host
[vivarium] environment k8s-host: /home/me/.local/state/vivarium/envs/k8s-host
$ vivarium shell k8s-host -- kubectl get pods -A
$ ssh -F ~/.local/state/vivarium/envs/k8s-host/ssh_config cp
$ vivarium ps
ready	k8s-host	k8s-host	/home/me/.local/state/vivarium/envs/k8s-host
$ vivarium down k8s-host
$ vivarium rm k8s-host
```

An environment has a name: the test's `name`, or `--name` for a second
one of the same test. Its directory is `envs/<name>` in the user's state
directory (platformdirs: `~/.local/state/vivarium` on Linux,
`~/Library/Application Support/vivarium` on macOS). `up` refuses a name
that is up. `--out DIR` instead of a name keeps the directory where you
say; `down --out DIR` stops it.

`up` starts `vivarium run <attr> --hold` in the background, with its
output in `<out>/run.log`. It returns when every phase passed and the
run says `ready`. It exits 0 and prints `<out>/access.json` on stdout. A
phase that fails pauses the run with the guests up, as
`--break-on-failure` does, and `up` exits 4.

The run detaches in a session of its own, but it stays in its caller's
cgroup. When that cgroup stops (a terminal's scope, a CI job, an agent's
session), the environment stops with it. `up --daemonize` runs it as a
transient unit of your systemd user manager instead, outside the caller:

```console
$ vivarium up checks.k8s-host --daemonize
[vivarium] started the run as vivarium-k8s-host.service, pid 378902, output in ...
$ systemctl --user status vivarium-k8s-host
```

The unit gets your environment by name, never by value on the command
line, and your working directory. `vivarium down` stops it as before;
`systemctl --user stop vivarium-NAME` does too, with the same teardown.
The unit ends when you log out, unless you enable lingering
(`loginctl enable-linger`); vivarium does not enable it. Without a
systemd user manager (macOS, most containers) `--daemonize` says so and
the run detaches as without it.

The way in is in `--out`, written before any guest boots:

- `access.json`: each guest's ssh address and forwards. The runner picks
  each forward's host port when the guest boots, so read it from here.
- `ssh_config`: `ssh -F <out>/ssh_config <guest>`, for a guest whose
  `vivarium.inbound` lists `{ guest = config.vivarium.sshPort; }`; no
  guest forwards ssh unless it asks. The root password is
  `vivarium.rootPassword` (`uml` by default).
- `access/`: what a phase writes for the host, through `vms.access`.
  `vivarium_k8s.bring_up` writes `access/kubeconfig` there when the
  control plane forwards 6443, which `modules/k8s.nix` does by default.
  The kubeconfig checks the name `kubernetes` in the server's
  certificate, not the forward address. The run empties `access/` when
  the guests go down.

A host port reaches only what `vivarium.inbound` names, and forwards are
fixed when a guest boots. An entry with no `host`, such as
`{ guest = 6443; }`, takes any free host port; `access.json` says which.
A fixed `host` that another process holds fails the boot and names the
port. `--random-ports` binds any free port for every entry, as a
sandboxed check does; the MCP `start` passes it, so runs beside each
other never collide. `ctl`, `switch` and
`reload` work on a ready environment as on a paused run. `continue` at
ready runs the phases a `reload` added, then holds again. Only `stop`
ends the environment.

Without the `vivarium` CLI, `nix run --file . checks.k8s-host.up -- --out
./k8s` runs the same environment in the foreground.

`down` stops it, waits for the run to exit and exits 0 if every phase
passed, and keeps its directory. `vivarium ctl --out <dir> stop` does the
same. A stop before `ready` is an ordinary stop: exit 5. `ps` (or `ls`)
lists every live run on this host, with its state: `ready`, `paused` or
`running`, or `unanswered` for a run that is evaluating or busy; then
each environment that is `down`. `rm NAME` deletes a stopped
environment's directory (`--force` stops it first, and refuses when it
is still up after that), and `clean` deletes every one that is down.

A run holds a lock on `<dir>/run.lock` from before it evaluates to its
exit; the kernel lets go of it however the run ends, SIGKILL included.
That lock, not a pid or a socket, says whether a run is live. `up`
refuses a directory whose run is live, by name or by `--out`.

A live run's sockets are in the user's runtime directory
(`$XDG_RUNTIME_DIR/vivarium/runs/<key>`, `~/Library/Caches/TemporaryItems`
on macOS), not in its directory, so none outlives a reboot. `vivarium monitor` exits 6 at `ready`. The MCP server's
`start` takes `hold=True`.

A sandboxed `nix build` of the same attribute never holds: it runs the
phases and gives the verdict, so the check proves the environment comes
up.

### Keep data across an environment's runs

Each run of an environment boots fresh guests: the root is the run's own.
A guest keeps the paths it lists:

```nix
nodes.cp.vivarium.persist = [ "/var/lib/data" ];
```

Each path is a bind mount of a directory on one store per guest,
`envs/<name>/disks/<guest>`. Under UML, QEMU and krun the store is a
sparse raw disk (`vivarium.persistSize` MiB, 16 GiB by default) that the
guest formats as ext4 on its first run. A container guest's store is a
host directory. A changed configuration still boots, on the kept data.
A store stays with its kind of backend: a disk does not boot under the
container backend, nor a directory under the others. `rm` deletes the
stores. Every other run gets a blank store that lasts
the run, so a check proves the first run (`checks.kept`).

`down` powers the guests off, so the disks are clean. A run killed with
SIGKILL leaves them as a power cut would.

A phase reads two things from `state.json` in the environment's
directory:

- `vms.state`: a dict of the phase's own keys. The run writes it after
  each phase. A value must be JSON; another fails the phase that set it.
- `vms.previous`: each phase's state in the environment's last run that
  reached a verdict, such as `{"init": "passed"}`.

A phase whose work is done ends with `vms.skip(reason)`. It shows as
`unneeded` in `phases.json`, with the reason. The phases after it run,
and the run can pass; `skipped` still means that a dependency failed.
Every other run, a sandboxed check included, starts with both empty and
writes nothing, so a check proves the first run.

```python
async def test(vms):
    if vms.previous.get("cluster") == "passed" and await kept_cluster_answers(vms):
        vms.skip("the cluster on the kept disks answers")
    ...
    vms.state["initialized"] = True
```

Check the guest as well as the state: the state describes the last run,
not what is on the disk now.

A Kubernetes node sets `services.vivarium-k8s.persist = true` and lists
nothing itself: the node keeps `/etc/kubernetes`, `/var/lib/kubelet`, its
runtime's images (`/var/lib/containerd`, or `/var/lib/containers` under
CRI-O), the control plane's `/var/lib/etcd` and its PersistentVolumes. `bring_up` sees
`admin.conf` on the kept disk and does not run `kubeadm init` or `join`
again; it waits for the API server and does the rest as before. A worker
that keeps nothing (`persist` on the control plane alone) joins again on
each run, in place of its old Node. A Node that no guest of the run is,
such as a worker removed from the test, is deleted. It refuses a kept
cluster made from
another kubeadm configuration (another Kubernetes version, address or
kubelet setting): kubeadm reads that only at init. `vivarium rm` the
environment then. `checks.k8s-kept` runs one environment twice. `checks.k8s-host` keeps its cluster:

```console
$ vivarium up checks.k8s-host
$ vivarium shell k8s-host -- kubectl create configmap probe
$ vivarium down k8s-host
$ vivarium up checks.k8s-host        # [k8s] cp: resumes the kept cluster
$ vivarium shell k8s-host -- kubectl get configmap probe
```

etcd then writes to a disk and not to a tmpfs, in every run.

### Load an image into a live cluster

`vivarium load` imports image tarballs into every node of a live run, as
`kind load image-archive` does (nixidae/nixidae#1591):

```console
$ docker save my-operator:dev > op.tar        # or a dockerTools image, or an OCI archive
$ vivarium load k8s-host op.tar               # or: vivarium load --out DIR op.tar
{
  "cp": [
    "docker.io/library/my-operator:dev"
  ]
}
```

stdout is the images each node now has, as the runtime names them.
`--node N` (repeatable) loads into those nodes only. A tarball may be
gzipped or not. One outside the store is linked into each node's
`/artifacts` for the import and removed after it; one in the store
reaches the node through its store view. A phase does the same with
`await vivarium_k8s.load_images(vms, [path])`.

Nothing serves the image, so a pod that runs it says
`imagePullPolicy: IfNotPresent` or `Never`. A `:latest` tag defaults to
`Always`, and `load` warns about it. A new image under the same tag
reaches only pods that start after the load: delete the running ones.
Each image must hold its own files. An image whose layers link into
`/nix/store` (`dockerTools` with `includeStorePaths = false`) goes in
`services.vivarium-k8s.extraImages`, which the node imports at boot.

With `persist`, a node keeps what was loaded: after `down` and `up` the
pods run again with no new load (`checks.k8s-kept`).

### Run several control planes

Give more than one node `services.vivarium-k8s.role = "control-plane"`
and put the run behind its router (nixidae/nixidae#1592):

```nix
mkTest {
  vivarium.recipes.kubernetes.enable = true;
  router.enable = true;
  nodes.cp1 = { services.vivarium-k8s = { enable = true; role = "control-plane"; }; };
  nodes.cp2 = { services.vivarium-k8s = { enable = true; role = "control-plane"; }; };
  nodes.cp3 = { services.vivarium-k8s = { enable = true; role = "control-plane"; }; };
}
```

The router balances the API endpoint, 10.0.2.4:6443, over the
control planes whose `/readyz` answers (`modules/k8s-api.nix`). Every
node reaches the API there (`controlPlaneEndpoint`). `bring_up` runs
`kubeadm init` on `cp_name`, or the first control plane by name, and
joins the others one at a time with `--control-plane`. Each runs etcd.
The host's `access/kubeconfig` goes through the router's forward, so it
outlives any one control plane. In an environment, the control planes
keep their cluster together or not at all: `persist` is the same on
each. `checks.k8s-ha` stops one API server and reads and writes through
the endpoint.

### Add a worker to a live cluster

Declare a template that makes a worker, on the cluster's segment, and
give the segment the addresses its instances take
(nixidae/nixidae#1592):

```nix
mkTest {
  vivarium.recipes.kubernetes.enable = true;
  nodes.cp = { services.vivarium-k8s = { enable = true; role = "control-plane"; };
               vivarium.lan = { network = "k8s"; address = "10.100.0.1/24"; }; };
  segments.k8s.instanceAddresses = [ "10.100.0.100/24" ];
  templates.worker = {
    services.vivarium-k8s = { enable = true; role = "worker"; };
    vivarium.lan.network = "k8s";
  };
}
```

Then add and remove workers while the run goes:

```console
$ vivarium node add k8s w1          # or: vivarium node add --out DIR w1 [--template worker]
{
  "ip": "10.100.0.100",
  "name": "w1"
}
$ vivarium node rm k8s w1
```

`node add` launches the instance, joins it with its own address as the
Node's InternalIP, and routes the pods of every node to it and back. It
returns when the Node is Ready. `node rm` drains and deletes the Node,
removes its routes and stops the instance. A phase does the same with
`await vivarium_k8s.add_node(vms, "worker", "w1")` and
`remove_node(vms, "w1")`. `--template` names the template when the run
has more than one.

A template's worker has no `persistentVolumes` and no `persist`: each
needs a fact the instance has only at run time. In a run with a router,
the instance sits behind it as every node does, so a cluster with several
control planes or a `loadBalancer` takes it too. The router leases it the
address of its index, and its Node carries that address in the label
`vivarium.dev/uplink-address`, which the router's LoadBalancer controller
routes to. When an environment comes up again,
`bring_up` deletes the Node of each instance, since no guest is it then.
`checks.k8s-add` adds two workers, checks their addresses and their pods,
and removes one.

### Reach a cluster's Services by name

`vivarium.recipes.kubernetes.loadBalancer.enable` puts the cluster behind
the run's router (`checks.k8s-lb`, nixidae/nixidae#1584):

- The router routes the cluster's Service and pod ranges to the nodes.
- `vivarium-lb` on the router gives each `type: LoadBalancer` Service an
  address from `loadBalancer.pool` (default `10.200.0.0/24`) and routes it
  to the nodes. The address shows as the Service's `EXTERNAL-IP`. Other
  guests of the run reach it too.
- The router serves a SOCKS5 proxy that resolves `cluster.local` names
  inside the lab. The host reaches it through a forward of the router's
  port 1080. The session serves a split proxy on the host in front of it:
  lab names (`vivarium.router.socks.domains`) and lab addresses go
  through the router's proxy, every other connection goes straight out
  from the host. The run exports it as `VIVARIUM_PROXY`, a `socks5h://`
  URL, and `vivarium shell` sets the proxy variables from it (see "Work
  in a shell that reaches the lab"):

```console
$ vivarium up checks.k8s-lb
$ vivarium shell k8s-lb
$ curl http://web.default.svc.cluster.local/
$ curl http://10.200.0.1/
```

`socks5h://`, not `socks5://`, makes the proxy resolve the name, not the
host. Chromium takes `--proxy-server=socks5://ADDR`, and Firefox needs
`network.proxy.socks_remote_dns`. A program that cannot use a proxy
cannot reach the cluster this way yet.

`vivarium.router.routes`, `vivarium.router.inside` and
`vivarium.router.socks` are the router's own options; the recipe sets
them. A lab without Kubernetes can set them on `nodes.router`.

### Trust the cluster's HTTPS

`vivarium.recipes.kubernetes.certManager.enable` runs cert-manager with a
ClusterIssuer `vivarium`. A `Certificate` or an Ingress that names it gets
a certificate the host can trust:

```yaml
cert-manager.io/cluster-issuer: vivarium   # an Ingress annotation
```

The issuer signs with a CA made for the run. Its root is yours, made once
in `~/.local/state/vivarium/ca` (platformdirs' state directory). The root's
key never leaves the host. Each run writes two files to `access/`:

- `ca.pem`: the root. Import it into a browser once; every later run
  chains to it.
- `ca-bundle.pem`: the host's public roots and the root, for programs that
  read one file.

The run exports `SSL_CERT_FILE` (OpenSSL, Go on Linux, Python's ssl),
`REQUESTS_CA_BUNDLE` and `CURL_CA_BUNDLE` as the bundle, and
`NODE_EXTRA_CA_CERTS` as `ca.pem`: Node adds it to its own roots.
`SSL_CERT_FILE` replaces a program's roots, so it names the bundle. In
`vivarium shell k8s-lb`:

```console
$ curl https://web-tls.default.svc.cluster.local/
```

Browsers read no variable: import `ca.pem` into their own store.
Chromium on Linux reads `~/.pki/nssdb`:

```console
$ certutil -d sql:$HOME/.pki/nssdb -A -t "C,," -n "vivarium lab root" -i ./k8s/access/ca.pem
```

Firefox has its own store: Settings, Certificates, View Certificates,
Authorities, Import, and trust it to identify websites.

Both CAs carry X.509 name constraints. They sign only for `cluster.local`,
`test`, `internal`, `localhost` and private addresses, so a cluster that
leaks the run's key cannot impersonate a public site. OpenSSL 3.5, Go
1.27, Chromium 157 and Firefox 160 enforce this (nixidae/nixidae#1584),
and `checks.k8s-lb` checks it with curl.

A test phase gets the same CA with `await vms.trust.issuer()`. A run that
never asks makes nothing.

### Work in a shell that reaches the lab

`vivarium shell NAME` starts your shell (`$SHELL`) with a live run's
variables, as `nix develop` does. `vivarium shell NAME -- CMD` runs one
command. `vivarium env NAME` prints the same variables as `export` lines:
`eval "$(vivarium env NAME)"` in bash, `eval (vivarium env NAME)` in fish.
`--json` prints one object. `--out DIR` names a run that is no named
environment. A run that is not up is refused (exit 3): its ports may be
another run's now.

| variable | from |
| --- | --- |
| `VIVARIUM_ENVIRONMENT`, `VIVARIUM_OUT` | the environment's name and directory |
| `KUBECONFIG` | `vivarium_k8s.host_kubeconfig` |
| `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `CURL_CA_BUNDLE`, `NODE_EXTRA_CA_CERTS` | the lab CA, once a phase asked for it |
| `VIVARIUM_PROXY` | the session's split proxy in front of the router's, `vivarium.router.socks` |
| `ALL_PROXY`, `HTTPS_PROXY`, `HTTP_PROXY` (both cases), `NO_PROXY` | `VIVARIUM_PROXY`; `NO_PROXY` keeps the host's own forwards direct |

A public download goes straight out from the host, so it works in an
`--offline` run too. A program that cannot speak SOCKS fails with the
proxy variables set: Python's httpx without `socksio` refuses every URL.
So `shell` and `env` put `unproxy` first on `PATH`: `unproxy CMD` runs
CMD without them. `nix`, the other `nix-*` commands, `pynix` and
`vivarium` run that way by themselves. `--no-proxy` leaves out the proxy
variables and `unproxy`.

A phase adds a variable with `await vms.export(NAME, VALUE)`. The run
keeps them, and `env` and `shell` ask the live run for them on its
control socket (`vivarium ctl --out O env` prints them as JSON).

## Drive guests from plain pytest

`pytest` in a shell with `packages.vivarium-eval.env` on PATH loads
vivarium's pytest plugin. A test names a `mkTest` attribute of the file
in `vivarium_file`, and gets its guests as fixtures
(`examples/pytest-host`):

```ini
# pytest.ini
[pytest]
vivarium_file = machines.nix
```

```python
@pytest.mark.vivarium("pair")              # scope="class" by default
class TestPair:
    async def test_reach(self, vms):
        await vms["a"].succeed("ping -c1 192.168.99.3")

@pytest.mark.vivarium("one", scope="function")
async def test_fresh(vm):                  # the run's only guest
    assert await vm.get("uplink_ip") == "10.0.2.15"
```

- The scope is pytest's: `function`, `class`, `module` or `session`. The
  plugin runs `vivarium up <attr> --file <vivarium_file>` for the first
  test of a scope, and `vivarium down` after its last.
- Each guest is a `vivarium.remote.RemoteMachine`. Each of its methods is a
  coroutine, and one round trip to the run's `machines.sock`, beside its control socket. It has
  the `Machine` methods that take and return plain data, such as
  `succeed`, `execute`, `wait_for_unit`, `wait_until_succeeds`, `journal`
  and the screen input methods. A list arrives as a tuple. `await
  vm.get("uplink_ip")` reads an attribute or property, and `vm.uplink_ip`
  raises an error that says so. `until` and `never` run their check in the
  test. `vms` is a plain dict, so it has no `vms.until`.
- A test that uses `vms` or `vm` is `async def`.
- `guest=` runs the test itself in that guest, fixtures and all, as
  root on the guest's kernel; sync or async, as any test:

  ```python
  pytestmark = pytest.mark.vivarium("one", guest="vm", scope="module")

  def test_as_root():
      assert os.getuid() == 0
  ```

  Every such test of one scope runs in one pytest in the guest, so a
  class or module fixture is made once. The guest gets this Python in
  its store view and a copy of the project at the same path, so imports
  and `__file__` match the host's. The host reports each test as its
  own, as it ends, and `-x` stops the guest too. A test that cannot
  import in the guest fails with the guest's collection error. Output
  pytest does not capture goes to `/artifacts/pytest/worker-*.log`.
  `vivarium closure` gives the dev shell's Python closure, so the
  `vivarium` CLI must be on PATH. With pytest-cov (`--cov`), the guest
  measures its tests the same way and pytest-cov's report includes
  them. Shortcuts taken are listed in `docs/design/pytest.md`.
- The runs are in `.vivarium/pytest/`, one `--out` each: `vivarium ctl`
  and the MCP server reach them as any run.
- `--vivarium-keep` leaves each run up, at `.vivarium/pytest/<attr>`, and
  the next `pytest` takes it as it is: no evaluation, no boot. One run
  per attribute then serves every scope. `vivarium down --out
  .vivarium/pytest/<attr>` ends it; do that after the machines change.
- pytest itself runs as you. Only the run is in vivarium's namespace.
- The plugin is `vivarium.pytest_host`, in the runner's own package. The
  `env` above adds the `vivarium` CLI, which evaluates `vivarium_file`.

The same suite as a sandboxed check, with every run built ahead:

```nix
checks.guests = vivarium.lib.pytestCheck {
  name = "guests";
  src = ./tests;                                  # where pytest runs
  runs = import ./tests/machines.nix { };         # each attribute a mkTest
};
```

In the sandbox nothing evaluates: the plugin starts each run from its
built `.up`, which `pytestCheck` names in `VIVARIUM_RUNS`. As with a
`mkTest`, `.attempt` keeps `log`, `junit.xml` and each run's `--out`
under `runs/`. `checks.pytest-host` is `examples/pytest-host` this way.

## Look at a guest's screen and use it

Give the guest a screen. It must be a QEMU guest:

```nix
nodes.desk = {
  vivarium.backend = "qemu";
  vivarium.display.enable = true;   # 1024x768; set width and height to change it
};
```

Then, in a phase or through `exec`:

```python
await desk.wait_for_text(r"login:")  # OCR; returns where the text is
await desk.send_chars("root\n")  # types on a US layout
await desk.send_key("ctrl-alt-f2")  # QEMU's sendkey names
await desk.click_text(r"^OK$")  # waits for it, clicks its middle
await desk.click(512, 384, button="right")  # pixels from the top left
await desk.drag((10, 10), (300, 200))
path = await desk.screenshot("after-login")  # a PNG in artifacts/desk/screenshots/
```

The MCP server's `screenshot(run, machine)` tool returns the image, so
an agent sees the screen and then acts on it through `exec`.

OCR misreads. Wait for lowercase words where you can: tesseract read
`VIVARIUM` in the console font as `UIUARIUM`. When a wait times out, the
error holds what OCR read and the path of a screenshot.

## Run a command in a guest

```python
done = await vm.succeed("ip -json addr")  # raises unless it exits 0
addresses = json.loads(done.stdout)
done = await vm.execute("test -e /etc/x")  # an exit code never raises
missing = done.returncode != 0
await vm.fail("test -e /etc/x")  # raises if it exits 0
```

Each returns a `CommandResult`: `returncode`, `stdout` and `stderr`. Each
stream loses its trailing newlines. A warning on stderr does not break a
parser of stdout. To read the two streams as one text, put `2>&1` in the
command and read `stdout`.

## Wait for a guest's state

Do not write a retry loop in a phase. Each `Machine` has these:

```python
await vm.wait_until_succeeds("ping -c 1 10.0.0.1", 90)  # until it exits 0
await vm.wait_until_fails("ping -c 1 10.0.0.1", 90)  # until it exits non-zero


async def up() -> dict[str, Any] | None:  # None: not yet
    peers = json.loads((await vm.succeed("vtysh -c 'show bgp summary json'")).stdout)
    return peers if all(p["state"] == "Established" for p in peers["peers"].values()) else None


peers = await vm.until("BGP up", up, 90)  # the first answer that is not None
await vm.never("a route back", route_back, 10)  # fails if it is not None within 10 s
await vms.until("BGP up", lambda vm: up_on(vm), 90)  # every guest at once
```

- `until` returns the first answer that is not `None`. `False` and `{}`
  are answers.
- At the timeout, `until` raises `MachineError`. Pass `evidence=` (an
  async function that returns text), and the error holds what it says.
- `never` always takes its whole timeout when it passes.
- A command past its own timeout raises `GuestTimeout`, a `MachineError`.
  The guest kills the command's process group, children included.
  `wait_until_succeeds` counts the attempt as failed and goes on.
  `wait_until_fails` raises it: a command that timed out did not exit.
- `vms.until` takes a check that gets the guest. `machines=` limits it to
  some guests. Its error names each guest that did not come good.
- Each wait is one step in `report.json`.

## Cut a link or capture its frames

Do it on the host, not in the guest: it then works the same on a guest
with no `nft` or `tcpdump`. The segment must be a hub
(`segments.core.hub = true`, or three members or more).

```python
await vms.segment("core").cut("leaf1")  # leaf1's frames on core are dropped; its carrier stays up
await vms.segment("core").restore("leaf1")
```

`segments.core.capture = true` writes `<out>/pcap/core.pcap` from boot.
A phase starts and stops one with `await vms.segment("core").capture()`
and `end_capture()`. By hand: `vivarium ctl --out O segment core cut leaf1`.
`docs/reference.md` has the rest.

## Find out what went wrong

`vivarium logs` prints a run's events as its `log` file shows them. It
takes an environment's name, a run's `--out` or its id, and works on a
live run, a stopped environment and a kept `.attempt` output:

```console
$ vivarium logs k8s-lb --guest cp --unit kubelet -n 20   # the last 20 kubelet lines on cp
$ vivarium logs k8s-lb --kind phase_finished --kind error
$ vivarium logs k8s-lb -f --guest client                 # then each new one, until the run ends
```

`--guest`, `--unit` (`kubelet`, `kubelet.service` or a syslog
identifier), `--kind`, `--phase`, `--case` and `--grep TEXT` narrow; a
filter given twice matches either value. `--json` prints each event's
line from `events.jsonl`. `--follow` reads new events until the run's
process ends. With no `events.jsonl` it exits 3.

The same with `jq`:

```console
$ jq -r 'select(.kind=="phase_finished") | "\(.phase)\t\(.data.state)"' out/events.jsonl
$ jq -c 'select(.kind=="journal" and .data.priority <= 3)' out/events.jsonl   # every error, every guest
$ less out/console/one.log                                                     # one guest's console
```

[reference.md](reference.md#what-a-run-leaves-behind) lists every file a
run writes.

## Run a test from an agent

With MCP, `vivarium-mcp` starts runs and reaches into them. nixidae's CLAUDE.md
lists its tools. `start` returns two monitor commands:

- `monitor`: pauses, failures and the verdict; exits 4 at a pause, 5 for a stopped run.
  Run it bare: a pipe or a `; echo` after it hides that exit status, and a
  pause then reads as success.
- `monitor_all`: every event, through pauses to the verdict.

Every tool takes a run's id or its `--out`. A run that another program
started, such as `vivarium up`, the pytest plugin or a shell, is attached
at first use, and `attach(out)` does the same explicitly. From then on its
new events arrive on the channel. The server never stops an attached run.
`runs` lists the runs the server reached and every other run up on this
host. `state` of a run that ended reads its `phases.json`.

An agent that only runs shell commands does not need MCP. With the
`vivarium` CLI, one command starts the run and waits:

```console
$ vivarium run checks.breakpoint --out ./out --break-on-failure --until-pause
$ vivarium ctl --out ./out continue --wait
```

`--until-pause` starts the run in its own session, with its output in
`<out>/run.log`, and exits at the first pause or at the verdict: 4 at a
pause, then 0, 1, 2 or 5 as the monitor exits. The run and its guests
stay up after a pause. `continue --wait` does the same from a pause.

With a `nix run` driver instead:

1. Start the run in the background, with its output in a file.
2. Start `vivarium monitor <out> --quiet` in the background at the same
   time. It waits for the run, and exits at a pause or at the verdict.
3. Read `<out>/run.json` for the commands: the run writes it before the
   first guest boots. Do not scrape the output file for them.
4. At a pause, copy the `exec` line from the file, change the Python,
   and run it.
5. Run the `continue` line with `--wait`. It exits at the next pause or
   at the verdict, so there is no monitor to start again.
