# The rewrite: diagnosis and proposal

Status: a proposal, 2026-11-08. Nothing here is decided. `runner.md` holds
what is decided; when a part of this is agreed, it moves there.

Evidence comes from two places:

- The code at nixidae `main` 4fce8457.
- 17 Claude Code sessions that used vivarium, 2026-10-11 to 2026-11-08,
  reduced to their vivarium moments and read in full. A quote below
  names its date. User words are verbatim.

## Summary

The phase model is sound. The trouble is around it:

1. The core knows about its modules. Kubernetes, the appliances and
   every backend have code in `lib.nix`, the spec and the runner.
2. The spec is a closed list. A new option of any kind crosses six
   layers, so each feature lands in the core.
3. A test is slow to iterate on, and most of the time is outside the
   test.
4. The pause-and-inject loop exists, but agents cannot find it or
   reach it. They rerun the whole test instead.
5. Phases have three gaps that consumers work around in their own Nix.

The proposal is one pivot: a spec that modules extend. Kubernetes, the
appliances, the cloud images and the backends then move out of the
core, one at a time.

## 1. The core knows about its modules

What the code shows:

- `lib.nix` `machineSpec` writes `kubernetes`, `cni`, `images` and
  `skipAddons` into every guest's spec. `MachineSpec` in
  `vivarium_runner/machine.py` has a field for each.
- `vivarium_runner/cluster.py` (700 lines of kubeadm bring-up) ships in
  the runner package. nixkube, easykubenix and solid-kubernetes import
  it from there.
- `modules/default.nix` imports `routeros-guest.nix`, `sonic-guest.nix`,
  `vyos-guest.nix`, `image-guest.nix` and `appliance.nix` into every
  guest. `appliance` is a value of `vivarium.backend`.
  `vivarium_runner` holds `routeros.py` and `sonic.py`.
- The backend list is written in `modules/default.nix` and again in
  `modules/run.nix`. `toolchainFor`, `featuresFor` and `machineSpec` in
  `lib.nix` branch on it. So do `guest.nix`, about ten sites in
  `qemu.nix`, `backend.py`, `spec.py` and `machine.py`
  (`self.spec.backend == "krun"`).
- `typeCheck` is a guest option, read from the first guest.
- The knob default env prefix is `UML_`.
- `modules/k8s.nix` still says a node knows only itself. `runner.md`
  decided the opposite: every guest knows its peers through `nodes`.

How it got that way: a fix for one consumer lands in the core, because
nothing else can hold it.

- The container launcher's cgroup layout changed for pynixd's resource
  test (2026-11-06).
- krun flags and a share allowlist for pynixd's build VM.
- An LKL nftables override for the IPsec lab (2026-10-19).
- `services.vivarium-k8s.kubeletSettings`, the `systems` passthru and
  the image-mode switch for nixkube (2026-10-26).
- `vivarium.image.writableStore` for havsorm's fleet (2026-10-11).
- `MachineSpec.images` and `skipAddons` for solid-kubernetes' offline
  cluster (2026-11-04).

Two consequences:

- The k8s module couples unrelated behaviour. `images = "nix" | "pull"`
  also sets IP masquerade and the image load unit, and it dropped
  `extraImages` silently (2026-10-26). `bring_up` assumed every guest is
  a node, so the cache guest waited on a CRI target.
- Siblings reached into the tree by path
  (`sources.vivarium + "/modules/k8s.nix"`) until 2026-10-29. Their
  replacement outputs, `nixosModules.k8s`, `lib.cacheFacts` and
  `lib.k8sImages`, are exports of the core library.

## 2. The spec is a closed list

`pkgs/vivarium/vivarium/spec.py` is a strict model. A key it does not
name fails the run. One new uplink option, `uplink.proxy`, crossed six
layers (2026-11-03): the option in `modules/default.nix`, `machineSpec`
and `toolchainFor` in `lib.nix`, the JSON, `spec.py`,
`MachineSpecJson` and `Toolchain` in `machine.py`, and `backend.py`.
The `libkrun` to `krun` rename missed one of them (2026-11-02).

When a layer is missed, the error says "the spec has fields this runner
does not know: gvproxy. A newer vivarium wrote it". Writer and reader
were the same tree both times.

So nothing outside the core can add a field. That is the mechanical
cause of section 1.

## 3. Iteration is slow

The user, verbatim:

- 2026-10-20: "The suite can't take 35 minutes man, that's the point."
- 2026-10-26: "It's not OK that a test suite takes 45 minutes + on this
  machine, we __need__ to optimize this."
- 2026-10-26: "figuring out how to probe more things for progress
  instead of "waiting in the dark"" (nixidae/nixidae#1092).
- 2026-11-02: "How do we speed up the iteration loop? 40+ minute tests
  just doesn't work".
- 2026-11-05: "negative absolutely only needs to test the individual
  test we expect to fail".

Measured causes, from the sessions:

| cause | cost | state |
| --- | --- | --- |
| `settings` in every guest's Nix database, so a source edit rebuilt every image | 4m48s for a no-change rebuild | fixed, nixidae/nixidae#1182 |
| shutdown waits on "Save Transient machine-id to Disk" | about 96 s a run | open, #1182 |
| `cp -r` of a source tree into `/work` | about 28 s each | open |
| `bring_up`'s `until` slept 10 s between 0.1 s checks | umlTest 1088 s to 701 s at 1 s | fixed |
| `vm.succeed` after a reboot waits its whole 130 s timeout | 520 of 577 s in boot-counting | fixed in the test only |
| fixed 300 s deadlines, no progress signal | a stall reads as slow | open, #1092 |
| no phase timeout | a hung phase waits for the user | open |
| `--only` boots every guest | the whole boot for one phase | open |
| a 60 GiB sparse root image sent to a remote builder | 14 and 30+ minute stalls | fixed |

The heaviest workload is pynixd's guest test. One session ran it 174
times, nearly always as `--only prepare --only <suite>`. A suite inside a
guest cannot be narrowed by vivarium: `-- -k` reaches only a pytest
phase that runs on the host (`session.py`). pynixd added its own
`PYNIXD_SUITE_ARGS`, which splits on spaces.

## 4. The pause loop is out of reach

The intended loop is: `driverDebug`, then `vivarium monitor`, then
`ctl exec` or `inject` at the pause. Across the 17 sessions:

- zero calls to a `mcp__vivarium__*` tool;
- `driverDebug` in a few sessions; full reruns in nearly all of
  them (75, 174 and "dozens" in three pynixd sessions);
- `vivarium switch`, which changes a guest's NixOS module live, used
  nowhere.

The user had to say it (2026-11-02): "you can use vivarium monitor to
get notified when something errors and with vivarium ctl you can send
Python into the machines to investigate the error instead of
"troubleshooting blind"".

Part of this is agent habit. Most of it is reach:

- `vivarium` is not on PATH. `packages.vivarium` was renamed to
  `packages.cli`; `packages.cli` rebuilds on use (about 3 minutes). Agents
  scrape the store path out of `run.log` (nixidae/nixidae#852). The
  rename also broke croshome's evaluation for three days.
- The MCP server registered only for a session started in `vivarium/`
  until 2026-11-02, because the root `.mcp.json` was ignored.
- `ctl` has no `stop` (nixidae/nixidae#860). Agents kill the driver by
  PID, or by pattern.
- `ctl state` names the failed phase, not the reason. The reason
  (`VFS: Mount too revealing`) came only from `exec` (2026-11-08).
- `inject` reloads the phase but not its helpers (nixidae/nixidae#851),
  and cannot change a module. A module change means a new run.
- A wrapper `vivarium monitor ...; echo rc=$?` exits 0 at a pause. A
  paused run then reads "completed (exit code 0)". Seen in five
  sessions.
- `ctl exec` dies when its client goes away (nixidae/nixidae#1166), and
  the agent channel died after 159 large answers (#1178).

The user's 2026-11-05 question ("a workflow where we don't have to wait
for the negative controls ... "pyjj run" or something maybe?") and
nixidae/nixidae#956 (an explore mode) ask for what vivarium partly has
already.

## 5. Phases: keep the model, close three gaps

What worked with no workaround (havsorm, 2026-10-11): `phases.<n>.nodes`,
`vms.phase`, `vivarium.configurations`, `settings` as data, `always`.

The gaps:

- `--only` drops `after` edges (nixidae/nixidae#661). nixkube wrote its
  own `phaseFilter` in Nix with a `NIXKUBE_UML_PHASES` knob that re-wires
  the edges, and `--break host` then paused before `cluster`
  (2026-10-26).
- `--break` needs a declared phase. The IPsec lab declared a no-op
  `lifecycle.py` only to pause before it.
- `settings` does two jobs: data for a phase, and paths registered in
  every guest. nixkube wraps values in `unsafeDiscardStringContext` to
  get the first without the second.

Smaller: backends are chosen by a knob (`PYNIXD_GUEST_BACKEND`,
`NIXKUBE_UML_BACKEND`) rather than `.qemu`, because a knob also sets
other things. `default.nix` is 2854 lines with about 50 tests inline.

## Proposal

### The pivot: a spec that modules extend

The spec gets a fixed core and named sections. A guest module writes
its section; the Python that owns the section reads it. The core passes
a section through without reading it.

```nix
# in a guest module
vivarium.spec.sections.kubernetes = { role = "worker"; cni = "bridge"; };
vivarium.spec.tools.gvproxy = lib.getExe' pkgs.gvproxy "gvproxy";
```

```python
# in the package that owns the section
@dataclass
class KubernetesSection:
    role: str
    cni: str

cfg = vm.spec.section("kubernetes", KubernetesSection)
```

Strictness stays, per section: a section's owner rejects a key it does
not know. A section with no owner is an error at start, which names the
module that wrote it.

Backends use the same mechanism. A backend is a guest module (its Nix:
kernel, boot, toolchain, required system features) plus a Python class
registered by name. `lib.nix` then asks each guest's backend for its
tools and features, instead of branching on the name.

### What moves out of the core

| now | after |
| --- | --- |
| `modules/k8s.nix`, `k8s-images.nix`, `cluster.py`, k8s fields in `MachineSpec` | `vivarium-k8s`: a module, a recipe that adds the `cluster` phase, and its Python. Nodes know their peers through `nodes`, as `runner.md` decided. Join and pod routes become units that wait on a file the runner writes (`runner.md` open item 4) |
| `routeros-guest.nix`, `sonic-guest.nix`, `vyos-guest.nix`, `appliance.nix`, `routeros.py`, `sonic.py` | `vivarium-appliances`: imported by the guests that use them |
| `image-guest.nix`, `image.nix`, `images.nix` | a guest kind module, beside the appliances |
| the backend enum and its branches | one module and one class per backend; the core ships the four as the default set |
| `typeCheck` on the guest | an `mkTest` option |
| `settings` registering paths | `settings` is data only. `closure` (new) names paths to register |

Each stays in the vivarium source; "out" means out of the core's
imports, its spec and its runner package. Consumers import
`vivarium.nixosModules.k8s` and `vivarium_k8s.cluster`, as they import
`nixosModules.k8s` and `vivarium_runner.cluster` today.

### Phases

- `--only` keeps the `after` closure, and boots only the guests those
  phases name.
- `--break` takes any point: a phase, `start`, or `failure`.
- `timeout` per phase, default unset.
- A guest-suite recipe: pytest inside a guest, junit to `/artifacts`,
  and `-- <args>` passed through to it. pynixd's `suite.py` is the
  first user.

### The agent surface

- `packages.vivarium` is the one CLI, on PATH in every dev shell that
  runs a test. `run` writes `<out>/run.json` first: the CLI path, the
  control socket, the commands. Nothing scrapes `run.log`.
- `ctl stop`, and `ctl state` with the failure text.
- `monitor` documents that a wrapper must not swallow its exit code.
- `inject --reload` drops the helpers it imported, and `switch` appears
  in the commands a pause prints.

### Iteration speed

The open rows of the table in section 3, measured before and after, on
pynixd's guest test with one unit test as the workload.

## Order of work

Each step keeps `checks` green and moves the consumers with it. No step
needs the next.

1. Spec sections and tools, with the core fields unchanged.
2. Kubernetes onto a section, then out of the runner package.
3. Appliances and image guests out of the default imports.
4. Backends as modules.
5. The phase gaps, then the agent surface.
6. `default.nix` split into `tests/<name>/default.nix`.

Speed items from section 3 land when found; they do not wait on the
order.

## The decision

Two routes:

- **The pivot first** (recommended). Cost: steps 1 and 2 before any
  speed work. Gain: the in-guest suite recipe and the
  cluster units land once, in their final shape. On the current shape
  each lands in the core, and is written twice.
- **Speed first, on the current shape.** Cost: each fix grows the core
  that the pivot then takes apart. Gain: shorter runs this week.
