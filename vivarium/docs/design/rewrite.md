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

The proposal compares three architectures against the cases that went
wrong, and picks one: **owners and contracts**. Recipes read the
evaluated guests, so Kubernetes needs no field in the spec. A backend
and a guest kind each own a typed configuration that the core does not
read. Their Python arrives through the run's own environment. The user
chose this route ("pivot first") on 2026-11-08.

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

## 6. What the core acts on

The five layers of a run, and who may extend each one today:

| layer | what it is | open to a module? |
| --- | --- | --- |
| run | `mkTest`'s `evalModules`: phases, nodes, defaults, settings, knobs | yes: a recipe |
| guest | each node's NixOS evaluation, `vivarium.*` | yes: a NixOS module |
| spec | the JSON between Nix and Python | no: `spec.py` and `MachineSpec` are closed |
| host runtime | `Session`, `Machine`, `Backend`, `cluster`, dialects | no: one package, fixed registries (`BACKENDS`, `DIALECTS`) |
| host Python | the venv the wrapper runs | no: `cli` is one fixed venv |

A feature spans all five. Only the first two take a module, so the
other three are where a feature lands in the core.

Three mechanisms also describe one idea, "this guest is not a stock
NixOS guest with an agent": `backend = "appliance"`, the
`system.build.vivariumImageGuest` flag, and `boot.image.control`.
`machineSpec` branches three ways on them.

The rule the proposal follows: **each fact has one owner, and it
crosses into the core only when the core acts on it.** The core acts
on a guest's identity, its segments and forwards (resolved across all
guests at once), its store view, its control channel, its lifecycle and
its systems. It does not act on a kernel path, a CNI or a disk format.

## Three architectures

### A. Open spec sections

The earlier proposal. The spec gets named sections. A guest module
writes one, the core passes it through, and the Python that owns it
reads it with its own strict model.

### B. Owners and contracts

Three extension points, each with one owner per value:

1. **A recipe is the unit of a multi-guest feature.** It is a run
   module, as `cache` is. It reads the evaluated guests (the `systems`
   argument), and writes `nodes`, `defaults`, `phases`, `settings` and
   Python packages. A guest never writes to the run.
2. **A backend owns a typed launch configuration.** Its NixOS module
   writes `vivarium.launch = { backend = "qemu"; config = { ... }; }`,
   with its tool paths inside `config`, and adds to
   `vivarium.requiredSystemFeatures`. Its Python class owns a strict
   model of `config`. It also owns `launch`, the store delivery (view,
   writable view, allowlist), reboot detection and its capabilities
   (memory control, screen).
3. **A guest kind owns its control channel.** It is a NixOS module.
   RouterOS, SONiC, VyOS, cloud images and appliances each set their
   backend's disk and boot parameters, and `vivarium.control`:
   `agent`, a console dialect with its configuration, or `none` with a
   ready pattern.

Python code reaches a run through `python.packages`, an option of the
run and of the guest. `mkTest` collects both into one venv. The wrappers
run that venv, and the type check reads it. A backend or a dialect is a
Python entry point (`vivarium.backends`, `vivarium.controls`) in a
package there, so a registry is never edited.

The core's machine contract becomes: `name`, `index`, `resources`
(memory, cpus), `network` (interfaces, uplink segment, forwards),
`store` (closure, late closure), `systems` (toplevel,
configurations), `launch` and `control`. The core validates its own
fields strictly. The owner validates `launch.config` and
`control.config`.

### C. A launcher program per guest

The shape nixos-test and microvm.nix use. Each guest's evaluation
builds its own launcher, which closes over its tools. The core runs it
through a small fixed interface: fds, the artifacts directory, the
store view, the forwards. The launcher reports its capabilities as
socket protocols (QMP, mconsole). Container (`crun_launch`), krun
(`krun-launch`) and UML (the passt bridge) are partly in this shape
already.

## Against the cases

Each row is a change that landed, or will land. A cell says where the
change goes. "core" means the core's imports, its spec or its runner
package.

| case | A: sections | B: owners | C: launchers |
| --- | --- | --- | --- |
| cgroup layout for pynixd (container) | core: `backend.py` | the container backend's config | the container launcher |
| krun flags and share allowlist | core, unless backends use sections too | the krun backend's config | the krun launcher |
| LKL nftables override | a NixOS module the IPsec lab imports; the same in all three. The core imports `nftables.nix` into every guest today, but nothing forces it | same | same |
| `kubeletSettings` | the k8s section | the k8s recipe's guest module | core: C does not cover a recipe |
| `writableStore` for cloud images | an image section | the image guest kind | the image guest kind |
| `images`, `skipAddons` for the cluster phase | the k8s section | `settings`, from the guests the recipe reads | core: as for `kubeletSettings` |
| a fifth backend | a section, and the `lib.nix` branches stay | a module and an entry point | a module that builds a launcher |
| a guest kind like VyOS | a section, and `machineSpec`'s branch stays | a module that sets `launch` and `control` | a module, but the dialect stays in core |
| Python for the feature | no route: it stays in `vivarium_runner` | `python.packages`, one venv for run and check | the launcher's own; a dialect has none |
| a misuse names its owner | only if each section records its writer | the spec carries the option's file (`options.*.files`) beside `launch` and `control` | the launcher's own error |

A leaves the backend rows half done and has no answer for Python. C
fixes the backend rows best. But it does nothing for recipes or
dialects. It also costs a protocol for each capability, and a launcher
process with the same configuration as the in-process class. The
backends share helpers (`Qemu._passt` and `Qemu._scratch_disk` serve
krun too), so each launcher would carry a copy of them or a shared
library. B covers every row. B also keeps C open: a backend may exec its
own launcher, as container and krun do now.

**Recommendation: B.** C is the later step for one backend, when a
second language or a host without Python needs it.

### Evidence for B's riskiest step

A recipe that reads `systems` to choose a phase's `nodes` recursed:
`mkTest` built the guests from the checked config, and the check reads
the phases. Measured on 2026-11-08 with a 12-line `mkTest`. The guests
now evaluate from the unchecked config, and the same `mkTest` selects
only the guest with `services.nginx.enable` (commit "vivarium: guests
evaluate from the unchecked run config"). The consumer tests that were
evaluated (`checks.recipes`, `checks.parallel`, `checks.k8s`, pynixd's
`tests.guest`, nixkube's `ciTest`) still evaluate.

### What the consumers use

From a survey of every directory outside `vivarium/`:

- k8s: `nixosModules.k8s` in three files (nixkube twice, easykubenix
  once), and `vivarium_runner.cluster` in seven nixkube files. No
  consumer reads `vm.spec.kubernetes`, `cni`, `images` or
  `skipAddons`.
- No consumer sets `vivarium.appliance`, `routeros`, `sonic` or `vyos`.
  Only vivarium's own tests and `examples/` do. havsorm and huggorm use
  `vivarium.image` with `vivarium.images`.
- No consumer sets `typeCheck` or relies on the `UML_` knob prefix;
  each knob names its own `env`.
- Consumers call 12 `Machine` methods. `succeed` (123 calls) and
  `execute` (45) dominate. Nothing reaches a private name or a path
  inside `vivarium/`.

So Kubernetes moves with three consumer files and seven imports, and
the guest kinds move with no consumer change beyond an `imports` line.

### Rejected, and why

- **A guest adds a phase to the run** (`runner.md` open item 3). The
  run would then depend on the guests and the guests on the run. A
  recipe that reads the guests does the same job in one direction, and
  a consumer overrides it in one place, with `lib.mkForce` on the run.
- **Session hooks** (a pluggy-style `on_boot`, `on_phase`). No case
  above needs one: `cluster` and `cache` are phases.
- **`backend = "appliance"`.** An appliance is a guest kind under the
  QEMU backend. `vivarium.backend` names the machine that runs the guest
  and nothing else.

## The phase model

The model of `after`, `nodes` and `always` stays. One change goes
deeper than §5: **a phase's guests boot when the phase needs them.**
Today every guest boots before the first phase. Under the change:

- `--only X` runs X and its `after` closure, and boots only their guests
  (nixidae/nixidae#661).
- A phase starts when its own guests are up, not the slowest guest of
  the run.
- A guest names the guests it cannot boot without:
  `vivarium.requires = [ "cache" ]`. The scheduler boots that closure
  with a phase's guests. The recipe that adds the dependency also
  writes it. `cache` sets it in `defaults`, beside the
  `vivarium-cache-online` unit that waits 120 s for the cache guest.
  The k8s recipe sets it on each worker. Without it, `--only check`
  on `nodes = [ "a" ]` would boot `a` alone, and `a` would fail at
  that unit's timeout.
- Readiness (`systemctl is-system-running --wait`, or a dialect's
  prompt) is a step of a guest's start, through its control channel.
  The `boot` phase stays as a name every `after` can still give, so no
  consumer changes.

The rest of §5:

- `--break` takes any point: a phase, `start`, or `failure`.
- `timeout` per phase, default unset.
- A guest-suite recipe: pytest inside a guest, junit to `/artifacts`,
  `-- <args>` passed through. pynixd's `suite.py` is the first user.
- `settings` is data only. A guest's `vivarium.lateClosure` names the
  paths its database registers after boot, and replaces the
  registration of `settings` in every guest.

## The agent surface

vivarium is Nix and Python. A test runs with `nix build` and `nix run`,
and the runner a test's spec names never evaluates. The CLI a person or
an agent runs is built on huggorm: it evaluates in its own process, so
it can evaluate a run again, switch a guest and reload a live run
(user, 2026-11-09). One program, `vivarium`, carries every command:
today's `vivarium-eval` (run by name, `phases`, `switch`) with `ctl`,
`monitor` and `stop` from the runner's library. The runner binary in a
spec takes an internal name. nixidae/nixidae#1254 adds `vivarium.nix`
as the default entrypoint.

- `packages.vivarium` is the one CLI, on PATH in every dev shell that
  runs a test. `run` writes `<out>/run.json` first: the CLI path, the
  control socket, the commands. Nothing scrapes `run.log`. Done, with
  typed `RunFile` and `Command`.
- `ctl stop`, and `ctl state` with the failure text. `stop` is done: a
  typed `Stop`, exit 5, verdict `stopped`.
- `monitor` documents that a wrapper must not swallow its exit code.
  Done.
- `inject --reload` drops the helpers it imported, and `switch` appears
  in the commands a pause prints. Done: `--reload DIR`, and `switch`
  for a run that `vivarium run <attr>` started.

## Order of work

Each step keeps `checks` green and moves the consumers with it.

1. `python.packages` on the run and the guest, one venv for the
   wrappers and the check. `typeCheck` moves from the first guest to
   the run. Done: `checks.python-packages`.
2. Kubernetes as `vivarium.recipes.kubernetes`. It imports the k8s
   module into every guest, reads the nodes from `systems` and writes
   their facts to `settings.kubernetes.nodes`. The k8s fields leave
   `machineSpec` and `MachineSpec`. nixkube and easykubenix move with
   it. `vivarium_k8s` is its own package, and the recipe puts it in
   the run's venv. Done. The recipe adds no `cluster` phase: each of the five callers of `bring_up`
   passes its own arguments, in a phase that does more than that.
3. The backend contract: `launch` and `requiredSystemFeatures`, entry
   points, capabilities. One backend at a time, UML first. The global
   toolchain, `toolchainFor` and `featuresFor` go with the last one.
   Done for `launch`, `requiredSystemFeatures` and the toolchain: each
   backend has a strict config class, and `modules/tools.nix` names the
   paths. Backends are entry points in `vivarium.backends`. A phase reads
   `vm.systems`. Done.
   havsorm reads `vm.spec.toplevel` and `vm.spec.configurations`, which
   move under `systems`; it moves in this step.
4. Guest kinds: `control`, and the RouterOS, SONiC, VyOS, image and
   appliance modules out of the default imports. `appliance` stops
   being a backend. Done: dialects are entry points in
   `vivarium.controls`. A kind sets `vivarium.kind`, `rootImage`,
   `closure` and `control`, and writes its boot parameters into its
   backend's launch config (`root` under UML, `disk` under QEMU). A
   guest imports its kind from `vivarium.kinds`.
5. The phase model: guests boot on demand, then `--only`, `--break`,
   `timeout`, the guest-suite recipe, and the `settings` split. The
   split is done: `settings` is data, a guest names its late paths in
   `vivarium.lateClosure` (it gets `settings` as a module argument;
   `vivarium.lib.pathsIn` lists a value's paths), and the runner
   registers them only in a guest with a Nix database
   (`nixDatabase.loader`). Done also: `--only` keeps the `after` closure,
   `--break start|failure|end`, `timeout`, and `--only` boots only the
   guests its phases need (`Session.ensure`, `vivarium.requires`, the
   `boot` recipe's `nodesFromDependents`). A full run still boots every
   guest first. The guest-suite recipe is `vivarium.recipes.suites`, and
   pynixd's suites run through it. Open: a phase that starts when its
   own guests are up in a full run (readiness in `Machine.start`),
   nixidae/nixidae#1229.
6. The agent surface. Done: `run.json`; `ctl stop` (exit 5, verdict
   `stopped`); `ctl state` with the pause reason and each error, and
   `ctl --json`; the monitor's exit status documented; `inject --reload
   DIR`; a `switch` line for a run started by name. The CLI on PATH is
   the huggorm `vivarium`, the runner is `vivarium-runner`, and the
   pynixd, nixkube and easykubenix dev shells carry the CLI. Every check
   body with logic is Python in `tests/checks/`. Next for the CLI:
   `vivarium.nix` as the default entrypoint, nixidae/nixidae#1254.
7. `default.nix` split into `tests/<name>/default.nix`.

Speed items from section 3 land when found; they do not wait on the
order.
