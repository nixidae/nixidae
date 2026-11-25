"""Bring a kubeadm cluster up on guests, and ask it questions.

A node knows nothing about the others -- see ``modules/k8s.nix`` -- so
everything needing the whole cluster in view lives here rather than in a
module: who joins whom, which /24 each node was given, the routes between
them, and whether anything is Ready.

This is a library and not a test.  ``tests/k8s.py`` is the three-node case,
and a phase outside this repository can build its own:

    from vivarium_runner import Machines
    from vivarium_k8s import bring_up, kubectl

    async def test(vms: Machines) -> None:
        cp = await bring_up(vms)
        await kubectl(cp, "apply --filename /nix/store/...")

`vivarium.recipes.kubernetes` puts this package in the run's venv and
writes the nodes to the run's settings, where `bring_up` reads them.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, TypedDict, cast

import anyio
from vivarium_runner.machine import Machine, MachineError
from vivarium_runner.machines import Issuer, Machines

from .images import ImageError as ImageError, image_refs, latest, listed_as, store_path


class NodeFacts(TypedDict):
    """One node's `services.vivarium-k8s`, as `vivarium.recipes.kubernetes`
    writes it to `settings.kubernetes.nodes`."""

    cni: str
    images: str
    skipAddons: list[str]  # noqa: N815 -- Nix writes it, so Nix spells it


def nodes(vms: Machines) -> dict[str, NodeFacts]:
    """The run's Kubernetes nodes, by name.

    The recipe lists every guest with `services.vivarium-k8s.enable`, so a
    cache guest, for one, is not here.
    """
    found = cast(dict[str, NodeFacts], vms.settings.get("kubernetes", {}).get("nodes", {}))
    if not found:
        raise MachineError(
            "this run has no Kubernetes node: set vivarium.recipes.kubernetes.enable"
            " in mkTest and services.vivarium-k8s.enable on a guest"
        )
    return found


# These are stuck-detectors, and that is the only thing they are.
#
# Nothing here is slow.  A guest is a process, its store is the host's, and
# every image was imported before kubelet started -- so there is nothing to
# compile, nothing to pull and nothing to fetch.  A control plane that is
# working is serving inside a couple of minutes.
#
# Not 30, 20 and 15 minutes, on the theory that a failure here is "nearly
# always slowness rather than breakage".  That theory makes a bug look like a
# delay: a stuck kubeadm and a slow one are indistinguishable for half an
# hour, and the half hour is spent either way.  Short deadlines
# turn the same bug into a report with the pod list, the events, kubelet,
# containerd and the pod logs attached.
#
# If one of these fires on something that was genuinely still working, the
# answer is to find out what took the time and fix that -- not to raise the
# number.
INIT_TIMEOUT = 5 * 60
JOIN_TIMEOUT = 5 * 60
READY_TIMEOUT = 5 * 60

# A systemd unit reaching `active`.  Same reasoning, and the same number:
# these units start local programs against local files.
UNIT_TIMEOUT = 5 * 60

# A check is a kubectl call or two, about 0.1 s in the guest. At 10 s the
# sleep was the run: nixkube's chaos phase spent 53 of 594 s in commands,
# and each of its five waits per scenario rounded up to the next 10 s.
POLL = 1

# How often `until` says what it is still waiting for.
_SAY_EVERY = 30

# The taint kubeadm puts on a control plane so that nothing schedules there.
CONTROL_PLANE_TAINT = "node-role.kubernetes.io/control-plane"

# The two addon pods, as label selectors, for `bring_up`'s `addons`.
#
# **kube-proxy is not optional in the way it looks.** It makes ClusterIP
# Services reachable, and the cluster has one whether or not a test declares
# any: `kubernetes.default`, at the first address of the service subnet, is
# how everything inside a pod reaches the API server. Take kube-proxy away
# and nothing gets DNAT'd there, so any in-cluster client -- kubectl in a
# Job, a controller using its ServiceAccount -- fails to reach the apiserver
# at all. Measured: nixkube's init Job runs `kubectl get secret` and exits
# non-zero without it.
#
# CoreDNS genuinely is optional. In-cluster clients read
# KUBERNETES_SERVICE_HOST, which is an address rather than a name, so nothing
# resolves anything unless a test asks it to. Skipping it saves two pods on a
# one-CPU guest and a great deal of log noise, because a sandboxed CoreDNS
# spends its life timing out against an upstream resolver it cannot reach.
# What a node writes when it declares PersistentVolumes -- see
# `services.vivarium-k8s.persistentVolumes` and `provision_storage`.
STORAGE_MANIFEST = "/etc/vivarium-k8s/storage.yaml"
# `services.vivarium-k8s.runtimes` and `provision_runtimes`.
RUNTIME_MANIFEST = "/etc/vivarium-k8s/runtimes.yaml"
KUBEADM_CONFIG = "/etc/vivarium-k8s/kubeadm-config.yaml"

KUBE_PROXY = "--selector k8s-app=kube-proxy"
KUBE_DNS = "--selector k8s-app=kube-dns"

DEFAULT_ADDONS = (KUBE_PROXY, KUBE_DNS)


async def kubectl(cp: Machine, args: str, timeout: float = 120) -> str:
    """Run kubectl on the control plane; returns its stdout."""
    return (await cp.succeed(f"kubectl {args}", timeout=timeout)).stdout


async def get_json(cp: Machine, args: str, timeout: float = 120) -> dict[str, Any]:
    # kubectl prints the object's JSON; its keys are the Kubernetes API's, read where used.
    return cast(dict[str, Any], json.loads(await kubectl(cp, f"{args} --output json", timeout=timeout)))


async def until[T](
    what: str,
    check: Callable[[], Awaitable[tuple[bool, T]]],
    timeout: float,
    machine: Machine,
) -> T:
    """Poll *check* until it says yes; returns what it last saw.

    *check* returns ``(done, evidence)``.  The evidence is reported on
    both paths, because "timed out waiting for nodes" on its own says
    nothing about which node was not ready.

    It is also reported while waiting, so that a watcher can tell a slow
    thing from a stopped one without waiting for the deadline.

    The report names how long the evidence has been *unchanged*, which is
    the number that says whether anything is happening.  A pod that goes
    Pending -> ContainerCreating -> Running is working; the same three
    words for four minutes are not, and the elapsed clock alone cannot
    tell them apart.  That figure is in the failure too: "stuck for 290s"
    and "still moving, just slow" are different bugs and want different
    fixes.

    Timed as one step by `Machine.until`, because this is the only poll
    loop a cluster test has and it is most of the run: measured on
    nixkube's nine scenarios, 632 of 786 seconds were in here.
    """
    started = time.monotonic()
    said: float | None = None
    seen: object = None
    changed = started

    async def step() -> tuple[T] | None:
        nonlocal said, seen, changed
        done, evidence = await check()
        # A 1-tuple, so a check that is done with `None` as its evidence
        # still ends the wait.
        if done:
            return (evidence,)
        now = time.monotonic()
        if evidence != seen:
            seen = evidence
            changed = now
        if said is None or now - said >= _SAY_EVERY:
            said = now
            first = str(evidence).strip().splitlines()[:1]
            machine.say(
                f"[{machine.name}] {what}: {int(now - started)}s elapsed, "
                f"{int(now - changed)}s unchanged, {int(started + timeout - now)}s left"
                + (f" -- {first[0]}" if first else "")
            )
        return None

    async def stuck() -> str:
        return (
            f"the last {int(time.monotonic() - changed)}s with nothing changing.\n"
            f"Nothing here is slow enough for this to be patience: the images "
            f"are on the node and the store is the host's. Something is "
            f"stuck.\n{seen}\n" + await diagnose(machine)
        )

    (result,) = await machine.until(what, step, timeout, POLL, evidence=stuck)
    return result


async def unscheduled(vm: Machine) -> str:
    """The events, and a describe of every pod that is not Running.

    **A pod that never leaves Pending leaves no other trace.** It starts no
    container, so `crictl ps -a` does not list it; it writes no pod log, so
    the log dump below is empty for it; and the kubelet never sees it, so
    its journal says nothing either. The reason is the scheduler's, and the
    scheduler writes it to an Event and to `describe`.

    Measured on nixkube's `test-qemu-ci-cache`: two runs reported
    `pynixd-0: Pending` for 300s each, and everything this function did not
    yet collect said nothing at all about it.

    `execute` and not `succeed`: this runs on a machine that is already in
    trouble, and a diagnostic must never be the thing that fails.
    """
    # `--output name` gives `pod/<name>` and no namespace, so the namespace
    # comes from custom columns instead.
    columns = "NS:.metadata.namespace,NAME:.metadata.name"
    return (
        await vm.execute(
            "kubectl get events --all-namespaces"
            " --sort-by=.lastTimestamp 2>&1 | tail -n 60;"
            " echo;"
            " kubectl get pods --all-namespaces --no-headers"
            " --field-selector=status.phase!=Running,status.phase!=Succeeded"
            f" --output custom-columns={columns} 2>/dev/null"
            " | while read -r ns name; do"
            "     kubectl describe pod -n $ns $name 2>&1 | tail -n 30;"
            "   done"
        )
    ).stdout


async def diagnose(vm: Machine) -> str:
    """Everything worth knowing about a node that would not come up.

    A CI round trip on a cluster test is the better part of an hour, so a
    failure should answer the next question as well as the first one.
    kubelet says why it will not start; containerd says why a container
    would not; crictl says which ones exist; and the pod logs are where
    the control plane itself complains -- kubeadm's own output shows none
    of that, because from where it stands the apiserver simply never
    answered.
    """
    # A test's own pods, and only then the control plane.
    #
    # Not every pod log under one 400-line budget, which the control plane
    # wins every time: etcd narrates each slow read and kube-apiserver each
    # synced cache, so on a UML guest the two of them
    # produce more than the budget by themselves.  Measured -- a failing
    # nixkube DaemonSet whose init container was the whole question, and
    # whose log did not appear in the report at all.
    #
    # kube-system is still worth having, so it gets a budget of its own.
    # Second, because `tail` keeps the end.
    logs = (
        await vm.execute(
            "{ for f in /var/log/pods/*/*/*.log; do"
            "   case $f in /var/log/pods/kube-system_*) ;;"
            '               *) tail -n 40 -v "$f";; esac;'
            " done; } 2>&1 | tail -n 300;"
            " echo;"
            " tail -n 15 -v /var/log/pods/kube-system_*/*/*.log 2>&1 | tail -n 150"
        )
    ).stdout
    # containerd or crio: the unit `services.vivarium-k8s.cri` put behind the target.
    runtime = (
        await vm.execute("systemctl show --property Requires --value vivarium-k8s-cri.target")
    ).stdout.split() or ["containerd.service"]
    return (
        f"--- [{vm.name}] addresses and routes ---\n"
        f"{(await vm.execute('{ ip -brief addr; ip route; ip -6 route; } 2>&1')).stdout}\n"
        f"--- [{vm.name}] events, and the pods that are not running ---\n"
        f"{await unscheduled(vm)}\n"
        f"--- [{vm.name}] crictl ps -a ---\n{(await vm.execute('crictl ps -a 2>&1')).stdout}\n"
        f"--- [{vm.name}] kubelet ---\n{await vm.journal('kubelet.service', lines=80)}\n"
        f"--- [{vm.name}] {runtime[0]} ---\n{await vm.journal(runtime[0], lines=40)}\n"
        f"--- [{vm.name}] pod logs ---\n{logs}"
    )


async def wait_for_images(vms: Mapping[str, Machine]) -> None:
    """No node is any use to kubeadm until containerd has the images.

    `UNIT_TIMEOUT` and not a number of its own.  This unit reads tarballs
    off the host store and hands them to containerd, so it is bounded by
    a local disk and nothing else -- if it has not finished in five
    minutes it is not importing slowly, it is stuck.
    """
    async with anyio.create_task_group() as group:
        for vm in vms.values():
            group.start_soon(vm.wait_for_unit, "k8s-load-images.service", UNIT_TIMEOUT)


# Where kubeadm's own narration lands, so that a deadline does not throw it
# away.
#
# `execute` returns the output when the command returns, and a command that
# is killed at `timeout` returns nothing at all -- which is how a GHA run of
# this test failed with "timed out after 300 seconds" and not one word about
# which phase kubeadm was in.  A file on the node survives the kill.
INIT_LOG = "/tmp/kubeadm-init.log"


async def init_control_plane(cp: Machine, *, patch_proxy: bool = True) -> None:
    """`kubeadm init` on *cp*.

    *patch_proxy* gives kube-proxy the store, which a node needs when its
    images are symlinks into `/nix/store` and kubeadm installs kube-proxy.
    """
    cp.say("[k8s] cp: kubeadm init ...")
    # Redirected and then printed, rather than piped through `tee`: a pipe
    # would report tee's exit status, and /bin/sh here has no `pipefail`.
    command = f"kubeadm init --config {KUBEADM_CONFIG} --v=2 > {INIT_LOG} 2>&1; rc=$?; cat {INIT_LOG}; exit $rc"
    try:
        done = await cp.execute(command, timeout=INIT_TIMEOUT, label="kubeadm init")
    except Exception as err:
        raise MachineError(
            f"[cp] kubeadm init did not finish in {INIT_TIMEOUT}s: {err}\n"
            + (await cp.succeed(f"tail -n 60 {INIT_LOG} 2>&1 || true")).stdout
            + "\n"
            + await diagnose(cp)
        ) from err
    if done.returncode != 0:
        raise MachineError(f"[cp] kubeadm init failed (exit {done.returncode}):\n{done.stdout}\n" + await diagnose(cp))
    # A skipped kube-proxy has no DaemonSet to patch: kubectl answers "not found".
    if patch_proxy:
        await patch_kube_proxy(cp)
    cp.say("[k8s] cp: control plane is up")


# kube-proxy's store mount, as a strategic-merge patch.
#
# Every image on these nodes is symlinks into /nix/store, so every
# container built from one needs the store to resolve them.  kubeadm's
# `patches.directory` covers the four static pods and CoreDNS; kube-proxy is
# applied from a manifest baked into kubeadm and has no patch target at all,
# so it is patched here, once, right after init.
#
# Doing it this way rather than through containerd's `base_runtime_spec`,
# which would give *every* container the store in three lines: a node that
# hands the store to pods that never asked for it cannot be used to test
# anything that puts /nix into a pod.  It would pass with its subject
# switched off.
KUBE_PROXY_PATCH = json.dumps(
    {
        "spec": {
            "template": {
                "spec": {
                    "containers": [
                        {
                            "name": "kube-proxy",
                            "volumeMounts": [
                                {
                                    "name": "nix-store",
                                    "mountPath": "/nix/store",
                                    "readOnly": True,
                                }
                            ],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "nix-store",
                            "hostPath": {"path": "/nix/store", "type": "Directory"},
                        }
                    ],
                }
            }
        }
    }
)


async def patch_kube_proxy(cp: Machine) -> None:
    """Give kube-proxy the store, since kubeadm will not."""
    done = await cp.execute(
        f"kubectl --namespace kube-system patch daemonset kube-proxy --type strategic --patch '{KUBE_PROXY_PATCH}' 2>&1",
        timeout=UNIT_TIMEOUT,
    )
    if done.returncode != 0:
        raise MachineError(f"[cp] could not give kube-proxy the store (exit {done.returncode}):\n{done.stdout}")
    cp.say("[k8s] cp: kube-proxy has the store")


async def join(cp: Machine, workers: Sequence[Machine]) -> None:
    """Bring the workers in with a token minted for this run.

    The token and the CA hash come from kubeadm rather than being read
    out of /etc/kubernetes, but the join itself does not use the command
    line kubeadm prints -- see `vivarium-k8s-join`, which wraps them in a
    configuration carrying this cluster's timeouts.

    --config is not optional here, even though the cluster already
    exists and this command only mints a token.  Without it kubeadm
    defaults an InitConfiguration first, and defaulting always calls
    ChooseAPIServerBindAddress -- which asks the kernel which interface
    owns the default route and then wants a global address on it.  That
    tolerates finding no default route at all (it warns and uses
    0.0.0.0), but treats a default route whose interface has no global
    address as fatal: "unable to select an IP from default routes", on a
    control plane that is up and serving.

    Passing the config we ran init with avoids the question rather than
    answering it: ResolveBindAddress returns an advertise address that
    is already concrete without looking at an interface at all.
    """
    if not workers:
        return
    printed = (await cp.succeed(f"kubeadm token create --print-join-command --config {KUBEADM_CONFIG}")).stdout
    fields = printed.split()
    try:
        token = fields[fields.index("--token") + 1]
        # Pins the cluster CA, so a worker cannot be talked into
        # bootstrapping against something else answering on that address.
        digest = fields[fields.index("--discovery-token-ca-cert-hash") + 1]
    except (ValueError, IndexError):
        raise MachineError(f"[cp] could not read a join command out of:\n{printed}") from None
    if cp.ip is None:
        raise MachineError("[cp] has no address to join")
    endpoint = f"[{cp.ip}]:6443" if ":" in cp.ip else f"{cp.ip}:6443"

    async def one(worker: Machine) -> None:
        worker.say(f"[k8s] {worker.name}: kubeadm join ...")
        # Kept on the node for the same reason as `INIT_LOG`.
        log = "/tmp/kubeadm-join.log"
        try:
            done = await worker.execute(
                f"vivarium-k8s-join {endpoint} {token} {digest} > {log} 2>&1; rc=$?; cat {log}; exit $rc",
                timeout=JOIN_TIMEOUT,
                label="kubeadm join",
            )
        except Exception as err:
            raise MachineError(
                f"[{worker.name}] kubeadm join did not finish in"
                f" {JOIN_TIMEOUT}s: {err}\n"
                + (await worker.succeed(f"tail -n 60 {log} 2>&1 || true")).stdout
                + "\n"
                + await diagnose(worker)
            ) from err
        if done.returncode != 0:
            raise MachineError(
                f"[{worker.name}] kubeadm join failed (exit {done.returncode}):\n{done.stdout}\n"
                + await diagnose(worker)
            )
        worker.say(f"[k8s] {worker.name}: joined")

    async with anyio.create_task_group() as group:
        for worker in workers:
            group.start_soon(one, worker)


async def wire_pod_network(cp: Machine, vms: Mapping[str, Machine]) -> None:
    """Give each node its CNI config, and a route to everyone else's pods.

    kube-controller-manager hands out a /24 per node, but nothing sets up
    a data path: this is the whole CNI, and it is about as small as one
    gets away with.  A real cluster would run an overlay here; guests on
    one Ethernet segment can just route.  A single node needs no routes
    at all and still needs the CNI configuration, or kubelet stays
    NotReady with "cni plugin not initialized".
    """
    # `kubeadm join` returns once the node has registered, which is not
    # the same instant kube-controller-manager's IPAM has given it a
    # /24: reading the node list straight afterwards is a race that a
    # fast join loses.
    cidrs: dict[str, str | None] = {}

    async def assigned() -> tuple[bool, str]:
        nodes = await get_json(cp, "get nodes")
        cidrs.clear()
        cidrs.update({item["metadata"]["name"]: item["spec"].get("podCIDR") for item in nodes["items"]})
        missing = sorted(name for name, cidr in cidrs.items() if not cidr)
        return not missing and len(cidrs) == len(vms), (
            f"without a podCIDR: {', '.join(missing) or 'none'}; nodes: {', '.join(sorted(cidrs)) or 'none'}"
        )

    await until("every node to be given a podCIDR", assigned, JOIN_TIMEOUT, cp)
    cp.say(f"[k8s] pod subnets: {cidrs}")

    if set(cidrs) != set(vms):
        raise MachineError(f"[cp] the cluster has {sorted(cidrs)}, the test booted {sorted(vms)}")

    async def one(vm: Machine) -> None:
        await vm.succeed(f"vivarium-k8s-cni {cidrs[vm.name]}")
        for peer in vms.values():
            if peer.name != vm.name:
                await vm.succeed(f"ip route replace {cidrs[peer.name]} via {peer.ip} dev vec1")

    async with anyio.create_task_group() as group:
        for vm in vms.values():
            group.start_soon(one, vm)


async def wait_for_ready_nodes(cp: Machine, expected: int) -> None:
    async def check() -> tuple[bool, str]:
        out = await kubectl(cp, "get nodes --no-headers")
        ready = [line for line in out.splitlines() if " Ready " in f" {line} "]
        return len(ready) == expected, out

    await until(f"{expected} Ready nodes", check, READY_TIMEOUT, cp)
    cp.say(f"[k8s] all {expected} nodes Ready")


async def wait_for_pods(cp: Machine, selector: str, namespace: str = "kube-system") -> None:
    """Wait until every pod matching *selector* is Ready."""

    async def check() -> tuple[bool, str]:
        pods = await get_json(cp, f"get pods --namespace {namespace} {selector}")
        items = pods["items"]
        ready = [
            pod
            for pod in items
            if any(c["type"] == "Ready" and c["status"] == "True" for c in pod["status"].get("conditions", []))
        ]
        summary = "\n".join(f"    {pod['metadata']['name']}: {pod['status'].get('phase')}" for pod in items)
        return bool(items) and len(ready) == len(items), summary

    await until(f"pods {selector}", check, READY_TIMEOUT, cp)


async def wait_for_dns(vm: Machine, host: str = "registry.k8s.io", timeout: float = 120) -> None:
    """A node that pulls its images has to resolve a name first.

    kubeadm asks containerd for an image within seconds of the guest
    reaching multi-user, and the guest's resolver is configured from DHCP
    by systemd-resolved at about the same moment.  Lose that race and the
    first thing that happens is `failed to pull and unpack image
    "registry.k8s.io/etcd:3.6.8-0": ... lookup registry.k8s.io: no such
    host`, which reads as a broken registry rather than as a resolver
    that was not ready.  Measured on a GitHub runner, five seconds after
    boot; this host is slower to start the test and never lost it.

    **The evidence is vec0's address, not the resolver.**  The resolver
    is a constant now -- see `networking.nameservers` in
    modules/guest.nix -- so it is the same on a guest that resolves and
    on one that does not, and says nothing.  What differs is whether
    vec0 ever got its address: a guest with only a link-local address
    on vec0 has no route to any resolver, however good the address.
    That was the whole of it on a GitHub runner, under a report that
    named the resolver -- run 34943651620.

    Not `/etc/resolv.conf` either: on a systemd-resolved guest that file
    is the 127.0.0.53 stub whatever happens.
    """

    async def check() -> tuple[bool, str]:
        found = await vm.execute(f"getent hosts {host}")
        if found.returncode == 0:
            return True, found.stdout.strip()
        uplink = (await vm.succeed("ip -4 -brief addr show vec0 2>/dev/null | tr -s ' ' || true")).stdout
        return False, f"not yet, and vec0 has {uplink.strip() or '(no IPv4 address)'}"

    await until(f"{vm.name} to resolve {host}", check, timeout, vm)


async def untaint(cp: Machine) -> None:
    """Let workloads run on the control plane.

    kubeadm taints it so that nothing schedules there, which is right for a
    cluster with workers and leaves a single-node cluster unable to run
    anything at all.  The trailing ``-`` is kubectl's syntax for removing a
    taint.  A node without it, such as a kept control plane a first run
    untainted, is left alone: kubectl would fail with "not found".
    """
    if await _tainted(cp):
        await kubectl(cp, f"taint node {cp.name} {CONTROL_PLANE_TAINT}-")
    cp.say("[k8s] control plane will schedule workloads")


async def taint(cp: Machine) -> None:
    """Keep workloads off the control plane, as kubeadm does: for a kept
    cluster that an earlier run made schedulable."""
    if not await _tainted(cp):
        await kubectl(cp, f"taint node {cp.name} {CONTROL_PLANE_TAINT}:NoSchedule")
        cp.say("[k8s] control plane is tainted again")


async def _tainted(cp: Machine) -> bool:
    taints = await kubectl(cp, f"get node {cp.name} --output jsonpath={{.spec.taints[*].key}}")
    return CONTROL_PLANE_TAINT in taints.split()


async def provision_storage(cp: Machine, vms: Mapping[str, Machine]) -> None:
    """Apply the PersistentVolumes every node declares, if any does.

    A node writes ``STORAGE_MANIFEST`` when
    ``services.vivarium-k8s.persistentVolumes`` is above zero, and nothing when
    it is not -- so this asks the guests rather than taking an argument,
    and a test that wants storage says so in one place.

    Each node's file names volumes pinned to that node, so the file is
    read where it was written and piped to the one machine that has a
    kubeconfig.  Applying the same StorageClass once per node is
    deliberate and costs nothing: `kubectl apply` is idempotent.
    """
    for name in await _apply_node_manifests(cp, vms, STORAGE_MANIFEST):
        vms[name].say(f"[k8s] {name} offers storage")


async def provision_runtimes(cp: Machine, vms: Mapping[str, Machine]) -> None:
    """Apply the RuntimeClasses every node declares, if any does.

    ``services.vivarium-k8s.runtimes`` writes ``RUNTIME_MANIFEST``, on the same
    terms as ``provision_storage``.
    """
    for name in await _apply_node_manifests(cp, vms, RUNTIME_MANIFEST):
        vms[name].say(f"[k8s] {name} offers runtime classes")


async def _apply_node_manifests(cp: Machine, vms: Mapping[str, Machine], path: str) -> list[str]:
    """Apply *path* from every node that has it; return those nodes' names."""
    applied: list[str] = []
    for name, vm in vms.items():
        if (await vm.execute(f"test -e {path}")).returncode != 0:
            continue
        manifest = (await vm.succeed(f"cat {path}")).stdout
        await cp.succeed(f"kubectl apply --filename - <<'EOF'\n{manifest}\nEOF")
        applied.append(name)
    return applied


STATE_KEY = "vivarium_k8s"
"""This package's entry in `vms.state`: `{"kubeadm": <sha256 of the
control plane's KUBEADM_CONFIG>}`, written once `kubeadm init` succeeds."""

ADMIN_CONF = "/etc/kubernetes/admin.conf"
KUBELET_CONF = "/etc/kubernetes/kubelet.conf"


async def _has(vm: Machine, path: str) -> bool:
    return (await vm.execute(f"test -e {path}")).returncode == 0


async def _fingerprint(cp: Machine) -> str:
    return (await cp.succeed(f"sha256sum {KUBEADM_CONFIG}")).stdout.split()[0]


async def kept_cluster(cp: Machine, workers: Sequence[Machine], vms: Machines) -> list[Machine] | None:
    """The workers to join into the cluster *cp* kept, or `None` when *cp*
    kept none and the cluster starts blank.

    A node keeps kubeadm's state only with `services.vivarium-k8s.persist`,
    in a named environment. The fingerprint is the kubeadm configuration
    the cluster was made from: kubeadm reads it at `init` and nowhere else,
    so a kept cluster with another one would ignore this run's.
    """
    fingerprint = await _fingerprint(cp)
    joined = {vm.name for vm in workers if await _has(vm, KUBELET_CONF)}
    if not await _has(cp, ADMIN_CONF):
        if joined:
            raise MachineError(
                f"{sorted(joined)} kept a cluster that the control plane {cp.name} did not;"
                " remove the environment with `vivarium rm` and bring it up again"
            )
        vms.state.pop(STATE_KEY, None)
        return None
    recorded = vms.state.get(STATE_KEY, {}).get("kubeadm")
    if recorded != fingerprint:
        why = (
            "an earlier `kubeadm init` did not finish"
            if recorded is None
            else "it was made from another kubeadm configuration, and kubeadm reads that only at init"
        )
        raise MachineError(
            f"[{cp.name}] the kept cluster does not fit this run: {why}."
            " Remove the environment with `vivarium rm` and bring it up again"
        )
    cp.say(f"[k8s] {cp.name}: resumes the kept cluster")
    return [vm for vm in workers if vm.name not in joined]


async def wait_for_api(cp: Machine) -> None:
    """A kept control plane starts its static pods at boot; wait until the
    API server answers."""

    async def check() -> tuple[bool, str]:
        done = await cp.execute("kubectl get --raw /readyz 2>&1")
        return done.returncode == 0, done.stdout.strip()

    await until("the kept API server", check, INIT_TIMEOUT, cp)


async def bring_up(
    vms: Machines,
    cp_name: str = "cp",
    schedulable: bool | None = None,
    addons: Sequence[str] = DEFAULT_ADDONS,
) -> Machine:
    """The whole sequence, from booted guests to a cluster that works.

    Returns the control plane machine.

    `services.vivarium-k8s.images` decides how the nodes start, and must be
    the same on every node.  Under `"nix"` the node imports every image at
    boot and each one is symlinks into `/nix/store`, so this waits for that
    import and gives kube-proxy the store.  Under `"pull"` there is no
    import to wait for -- waiting would hang until the deadline on a unit
    nobody created -- and nothing needs patching.  It waits for a name to
    resolve instead, which is the thing a pulling node cannot start
    without.  See `wait_for_dns`.

    *schedulable* removes the control plane's taint.  The default decides by
    size: a single-node cluster has nowhere else to put a pod, and one with
    workers should keep the control plane for the control plane.

    *addons* are the addon pods to wait for, as label selectors.  Pass `()`
    for a cluster that runs neither -- see `DEFAULT_ADDONS`.  It has to
    agree with `services.vivarium-k8s.skipAddons`, which decides what kubeadm
    installs in the first place: waiting for a pod nobody created hangs
    until the deadline, and not waiting for one that exists lets a test
    start before cluster DNS answers.

    The nodes are what `vivarium.recipes.kubernetes` wrote to
    `vms.settings`: every guest with `services.vivarium-k8s` enabled. The
    rest of the run, a cache guest for one, is left alone.

    A cluster kept with `services.vivarium-k8s.persist` skips `kubeadm
    init` and the kube-proxy patch, and joins only a worker that kept
    nothing, in place of its old Node: see `kept_cluster`. It deletes a
    Node that no guest of this run is. The rest runs
    again and leaves what is already done, since the guests' roots, the
    host's forwards and the router start blank each run.

    Nodes with `services.vivarium-k8s.cni = "external"` get no pod network
    from here: the test deploys its own CNI. Until it does, no node is
    Ready and no pod-network pod runs, CoreDNS included, so this returns
    without waiting for either; host-network addons such as kube-proxy are
    still waited for.
    """
    facts = nodes(vms)
    absent = sorted(set(facts) - set(vms))
    if absent:
        raise MachineError(f"this phase does not hold the nodes {absent}; add them to its `nodes`")
    cluster = {name: vms[name] for name in facts}
    cnis = {node["cni"] for node in facts.values()}
    external = cnis == {"external"}
    if not external and "external" in cnis:
        raise MachineError("services.vivarium-k8s.cni differs between nodes; one cluster has one CNI")
    sources = {node["images"] for node in facts.values()}
    if len(sources) != 1:
        raise MachineError(f"services.vivarium-k8s.images differs between nodes ({sorted(sources)}); set one for all")
    cp = cluster[cp_name]
    workers = [vm for name, vm in cluster.items() if name != cp_name]
    if schedulable is None:
        schedulable = not workers

    for vm in cluster.values():
        await vm.wait_for_unit("vivarium-k8s-cri.target", timeout=UNIT_TIMEOUT)
    if sources == {"nix"}:
        await wait_for_images(cluster)
    else:
        for vm in cluster.values():
            await wait_for_dns(vm)

    joining = await kept_cluster(cp, workers, vms)
    if joining is None:
        await init_control_plane(
            cp,
            patch_proxy=sources == {"nix"} and "kube-proxy" not in facts[cp_name]["skipAddons"],
        )
        vms.state[STATE_KEY] = {"kubeadm": await _fingerprint(cp)}
        joining = workers
    else:
        await wait_for_api(cp)
        # A worker that kept nothing left its Node behind, maybe still
        # Ready: kubeadm refuses to join a second Node of that name. A
        # Node no guest is (a worker gone from the test, an instance a
        # live run added) would hold a podCIDR nothing routes.
        listed = (await kubectl(cp, "get nodes --output jsonpath={.items[*].metadata.name}")).split()
        stale = sorted({vm.name for vm in joining} | (set(listed) - set(cluster)))
        for name in stale:
            await kubectl(cp, f"delete node {name} --ignore-not-found --wait")
        if stale:
            cp.say(f"[k8s] {cp.name}: deleted the Nodes {stale}")
    await join(cp, [vm for vm in joining if vm in workers], [vm for vm in joining if vm in others])
    if not external:
        await wire_pod_network(cp, cluster)
        await wait_for_ready_nodes(cp, len(cluster))
    await provision_storage(cp, cluster)
    await provision_runtimes(cp, cluster)
    for selector in addons:
        if external and selector == KUBE_DNS:
            continue
        await wait_for_pods(cp, selector)

    # Every run, both ways: a kept cluster keeps whatever its last run left.
    if schedulable:
        await untaint(cp)
    else:
        await taint(cp)

    await host_kubeconfig(cp, vms)
    router = vms.settings.get("kubernetes", {}).get("loadBalancer", {}).get("router")
    if router is not None:
        await connect_load_balancer(cp, vms[router])
    cert_manager = vms.settings.get("kubernetes", {}).get("certManager")
    if cert_manager is not None:
        if vms.trust is None:
            raise MachineError("certManager needs the run's lab CA, and this session has none")
        await install_cert_manager(cp, cert_manager["manifest"])
        await connect_issuer(cp, await vms.trust.issuer(), cert_manager["issuer"])
    return cp


LOADS = "load"
"""Where a host tarball waits in a node's `/artifacts` while it is imported."""

IMPORT_TIMEOUT = 10 * 60
"""Seconds one node has to import what `load_images` gives it."""


async def load_images(vms: Machines, tarballs: Sequence[str | Path], on: Sequence[str] = ()) -> dict[str, list[str]]:
    """Import *tarballs* into the container runtime of the nodes *on*, by
    default every node: `docker save` or OCI archives, gzipped or not, each
    image whole. Returns the images each node now has, by node.

    A tarball in the host's store reaches a node through its store view;
    one outside it is linked into the node's `/artifacts` and removed after
    the import. A pod that runs a loaded image asks for its tag with
    `imagePullPolicy: IfNotPresent` or `Never`, as on kind. An image whose
    layers point into `/nix/store` (`includeStorePaths = false`) is no case
    for this: give it to `services.vivarium-k8s.extraImages`.
    """
    targets = list(on) or list(nodes(vms))
    absent = sorted(set(targets) - set(vms))
    if absent:
        raise MachineError(f"no node {absent} in this run")
    sources = [Path(tarball).resolve() for tarball in tarballs]
    try:
        refs = {source: await anyio.to_thread.run_sync(image_refs, source) for source in sources}
    except ImageError as error:
        raise MachineError(str(error)) from error
    loaded: dict[str, list[str]] = {}

    async def one(vm: Machine) -> None:
        staged: list[Path] = []
        inside: list[str] = []
        try:
            for source in sources:
                kept = store_path(source)
                if kept is not None:
                    await vm.add_paths([str(kept)])
                    inside.append(str(source))
                    continue
                folder = vms.artifacts / vm.name / LOADS
                target = folder / f"{uuid.uuid4().hex[:8]}-{source.name}"
                await anyio.to_thread.run_sync(_stage, source, target)
                staged.append(target)
                inside.append(f"/artifacts/{LOADS}/{target.name}")
            vm.say(f"[k8s] {vm.name}: importing {', '.join(source.name for source in sources)}")
            listed = await vm.succeed("vivarium-k8s-import " + shlex.join(inside), timeout=IMPORT_TIMEOUT)
        finally:
            for target in staged:
                target.unlink(missing_ok=True)
        present = set(listed.stdout.split())
        wanted = sorted({ref for found in refs.values() for ref in found})
        missing = [ref for ref in wanted if not listed_as(ref) & present]
        if missing:
            raise MachineError(f"[{vm.name}] the runtime lists no {missing} after the import:\n{listed.stdout}")
        loaded[vm.name] = wanted

    async with anyio.create_task_group() as group:
        for name in targets:
            group.start_soon(one, vms[name])
    for ref in sorted({ref for found in refs.values() for ref in found}):
        if latest(ref):
            vms[targets[0]].say(
                f"[k8s] {ref} is `latest`: a pod needs imagePullPolicy IfNotPresent or Never to run it, or kubelet pulls"
            )
    return loaded


def _stage(source: Path, target: Path) -> None:
    """*source* at *target*: a hard link, which costs no space, else a copy."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


APISERVER_PORT = 6443

APISERVER_NAME = "kubernetes"
"""A name kubeadm puts in every API server certificate. The host reaches
the server on a forward address, `127.0.0.N`, which the certificate does
not hold, so the kubeconfig checks this name instead."""


async def host_kubeconfig(cp: Machine, vms: Machines) -> Path | None:
    """`<out>/access/kubeconfig`: *cp*'s admin kubeconfig, aimed at the
    host's forward of the API server. `None`, with a note, when nothing
    forwards it: `modules/k8s.nix` forwards 6443 on a control plane unless
    the test sets its own `vivarium.inbound`."""
    reachable = cp.reachable(APISERVER_PORT)
    if not reachable:
        cp.say(f"[k8s] {cp.name}: no host forward of {APISERVER_PORT}; no kubeconfig for the host")
        return None
    config = json.loads((await cp.succeed("kubectl config view --raw --flatten --output json")).stdout)
    for cluster in config["clusters"]:
        cluster["cluster"]["server"] = f"https://{reachable[0]}"
        cluster["cluster"]["tls-server-name"] = APISERVER_NAME
    path = vms.access / "kubeconfig"
    # Owner only: it holds the cluster admin's key.
    await anyio.Path(path).touch(mode=0o600)
    await anyio.Path(path).write_text(json.dumps(config, indent=2) + "\n")
    await vms.export("KUBECONFIG", str(path))
    cp.say(f"[k8s] host kubeconfig: {path} (server https://{reachable[0]})")
    return path


LOAD_BALANCER_RBAC = """
apiVersion: v1
kind: ServiceAccount
metadata: {name: vivarium-lb, namespace: kube-system}
---
apiVersion: v1
kind: Secret
type: kubernetes.io/service-account-token
metadata:
  name: vivarium-lb
  namespace: kube-system
  annotations: {kubernetes.io/service-account.name: vivarium-lb}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata: {name: vivarium-lb}
rules:
- apiGroups: [""]
  resources: [services, nodes]
  verbs: [get, list, watch]
- apiGroups: [""]
  resources: [services/status]
  verbs: [patch, update]
---
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: vivarium-lb}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: vivarium-lb}
subjects:
- {kind: ServiceAccount, name: vivarium-lb, namespace: kube-system}
"""

LOAD_BALANCER_CREDENTIALS = "/run/vivarium-lb/serviceaccount"
"""Where `modules/k8s-lb.nix` waits for them: the token's arrival starts
the controller."""


async def connect_load_balancer(cp: Machine, router: Machine) -> None:
    """Give the router's LoadBalancer controller a ServiceAccount: what it
    may read and patch, nothing more. Its token, CA and namespace go to
    `LOAD_BALANCER_CREDENTIALS` on *router*, the token last."""
    await cp.succeed(f"kubectl apply --filename - <<'EOF'\n{LOAD_BALANCER_RBAC}\nEOF")

    async def token() -> tuple[bool, str]:
        out = await kubectl(cp, "--namespace kube-system get secret vivarium-lb --output jsonpath='{.data.token}'")
        return bool(out.strip()), out.strip()

    encoded = await until("the vivarium-lb token", token, READY_TIMEOUT, cp)
    ca = await kubectl(cp, "--namespace kube-system get secret vivarium-lb --output jsonpath='{.data.ca\\.crt}'")
    files = {"namespace": "a3ViZS1zeXN0ZW0=", "ca.crt": ca.strip(), "token": encoded}
    await router.succeed(f"install -d -m 0700 {LOAD_BALANCER_CREDENTIALS}")
    for name, value in files.items():
        # base64 in, decoded on the router: no quoting of a PEM in a shell.
        await router.succeed(
            f"echo {value} | base64 -d > {LOAD_BALANCER_CREDENTIALS}/{name}.part"
            f" && mv {LOAD_BALANCER_CREDENTIALS}/{name}.part {LOAD_BALANCER_CREDENTIALS}/{name}"
        )
    cp.say(f"[k8s] {router.name}: LoadBalancer controller credentials written")


CERT_MANAGER_NAMESPACE = "cert-manager"
CA_SECRET = "vivarium-ca"
"""The run's intermediate CA, in `CERT_MANAGER_NAMESPACE`: where
cert-manager reads a ClusterIssuer's secret."""


async def install_cert_manager(cp: Machine, manifest: str) -> None:
    """Apply *manifest* and wait until its webhook can admit an issuer.

    Server-side: the CRDs are larger than the annotation a client-side
    apply keeps of them."""
    await cp.succeed(f"kubectl apply --server-side --filename {manifest}", timeout=180)
    await cp.succeed(
        f"kubectl --namespace {CERT_MANAGER_NAMESPACE} wait --for=condition=Available deployment --all --timeout=300s",
        timeout=320,
    )
    cp.say("[k8s] cert-manager is available")


async def connect_issuer(cp: Machine, issuer: Issuer, name: str) -> None:
    """A ClusterIssuer *name* that signs with *issuer*.

    The key reaches the guest in the command's environment, which no
    event records, and leaves the disk once the Secret holds it. The
    Secret's chain ends in the root, so each issued Secret's `ca.crt` is
    the root a client trusts."""
    staging = "/run/vivarium-ca"
    await cp.succeed(
        f"install -d -m 0700 {staging}"
        f' && printf %s "$CHAIN" > {staging}/tls.crt && printf %s "$KEY" > {staging}/tls.key'
        f" && kubectl --namespace {CERT_MANAGER_NAMESPACE} create secret tls {CA_SECRET}"
        f" --cert {staging}/tls.crt --key {staging}/tls.key --dry-run=client --output yaml"
        " | kubectl apply --filename -"
        f"; status=$?; rm -rf {staging}; exit $status",
        env={"CHAIN": issuer.certificate + issuer.root, "KEY": issuer.key},
    )
    cluster_issuer = {
        "apiVersion": "cert-manager.io/v1",
        "kind": "ClusterIssuer",
        "metadata": {"name": name},
        "spec": {"ca": {"secretName": CA_SECRET}},
    }
    # The webhook may still refuse for a moment after its Deployment is Available.
    await cp.wait_until_succeeds(
        f"kubectl apply --filename - <<'EOF'\n{json.dumps(cluster_issuer)}\nEOF", timeout=READY_TIMEOUT
    )
    await cp.succeed(f"kubectl wait --for=condition=Ready clusterissuer/{name} --timeout=120s", timeout=130)
    cp.say(f"[k8s] ClusterIssuer {name} signs with the run's intermediate CA")


__all__ = [
    "ADMIN_CONF",
    "APISERVER_NAME",
    "APISERVER_PORT",
    "CA_SECRET",
    "CERT_MANAGER_NAMESPACE",
    "CONTROL_PLANE_TAINT",
    "DEFAULT_ADDONS",
    "INIT_TIMEOUT",
    "JOIN_TIMEOUT",
    "KUBEADM_CONFIG",
    "KUBELET_CONF",
    "KUBE_DNS",
    "KUBE_PROXY",
    "LOAD_BALANCER_CREDENTIALS",
    "LOAD_BALANCER_RBAC",
    "POLL",
    "READY_TIMEOUT",
    "RUNTIME_MANIFEST",
    "STATE_KEY",
    "STORAGE_MANIFEST",
    "UNIT_TIMEOUT",
    "NodeFacts",
    "bring_up",
    "connect_issuer",
    "connect_load_balancer",
    "diagnose",
    "get_json",
    "host_kubeconfig",
    "init_control_plane",
    "install_cert_manager",
    "join",
    "kept_cluster",
    "kubectl",
    "load_images",
    "nodes",
    "provision_runtimes",
    "provision_storage",
    "taint",
    "untaint",
    "until",
    "wait_for_api",
    "wait_for_dns",
    "wait_for_images",
    "wait_for_pods",
    "wait_for_ready_nodes",
    "wire_pod_network",
]
