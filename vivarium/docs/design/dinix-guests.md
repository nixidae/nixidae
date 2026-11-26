# dinix guests: design (proposal, 2026-11-17)

A guest with no NixOS in it: a kernel, an ephemeral root disk, the
host's store, and dinit as its service manager, configured with dinix
modules. For tests that want a clean, fast box and no NixOS services:
pytest's per-test boxes (nixidae/nixidae#1413, `pytest.md`) first.

`vivarium.kinds.dinix` boots under krun and in a container, and passes
`checks.dinix` and `checks.dinix.container`. QEMU, UML, the uplink and
the unit queries are not done (Order of work, below).

In a container, crun execs dinix's `containerWrapper` (`dinit
--container`) as PID 1, named by `vivarium.boot.init`. The OCI config
makes the mounts, so `dinix-boot` does not run. dinit reaps orphans as
PID 1 must, so nothing like tini stands in front of it. The agent is
ready 0.52 s after the launch, against 2.45 s under krun (2026-11-26).

A dinix guest is its own router: `uplink.proxy = "vsock"`, with
the NixOS guest's namespace, ruleset and relay (`modules/router.nix`,
`system.build.vivariumOwnUplink`) run as two dinix services. No router
guest boots with it. `checks.dinix.container` took 8.4 s with a router
and takes 2.2 s; one in-guest pytest run of `checks.pytest-worker-dinix`
took 14.3 s and takes 3.0 s, against 5.3 s for a NixOS container guest.
Under krun the kernel has veth, nftables and vsock as modules. With no
udev, the uplink service points `/proc/sys/kernel/modprobe` at the
store's modprobe for what nft asks for, and loads the vsock transport
itself. `checks.dinix` takes 4.7 s.

## What User asked for

- No NixOS in the guest, and no stage 1: direct kernel boot.
- The root filesystem is an ephemeral disk image, as every guest's is.
  Not tmpfs, which spends the guest's RAM.
- Services are dinix modules, so a test swaps them declaratively.
- dinix may grow the init that this needs (User's idea: an `init` binary
  in dinix).

## Where it sits in vivarium

A **guest kind**, as `rewrite.md` (B) defines one: a NixOS module whose
own system is never built, imported from `vivarium.kinds.dinix`. The
node's NixOS evaluation stays, only for vivarium's own options (memory,
cpus, interfaces, forwards), as for RouterOS, SONiC and cloud images.
Nothing of NixOS is built or booted. The kind sets what the core acts
on, and nothing else:

| fact | set to |
| --- | --- |
| `vivarium.kind` | `"dinix"` |
| `vivarium.rootImage` | its own ext4 image: the init, mount points, nothing else |
| `vivarium.closure` | the closure of the dinix service tree, the agent and the init's targets |
| `vivarium.nixDatabase.enable` | false: no Nix in the guest unless a module adds it |
| `vivarium.control` | `agent`: vivarium's agent runs as a dinit service |
| `vivarium.boot` | the kernel, its command line and the root image (below) |

The core's per-guest work stays unchanged: the store view or allowlist,
`/artifacts`, the agent channel, forwards, segments, the uplink egress,
the console.

## The boot chain

1. The backend boots the kernel directly: `root=/dev/vda rw
   init=/dinix-boot DINIX_SPEC=/dinix-boot.spec`. No initrd: krun's
   launcher takes `--initrd` as optional, and libkrun's C API maps a
   NULL string to no initrd (ffier's `Option<&str>`).
2. `/dinix-boot`, on the root disk, follows the spec: `/proc`, `/sys`,
   `/run`, the store (virtiofs `nix` with an overlay on krun and QEMU,
   hostfs on UML, as each backend's NixOS boot does), and `/artifacts` as
   an optional mount. Then it execs dinit from the store. Every directory
   it mounts on is in the image already. The kernel mounts `/dev` itself
   (nixpkgs sets `DEVTMPFS_MOUNT`), and a second devtmpfs mount fails
   with EBUSY.
3. dinit, as PID 1 in system mode, runs dinix's tree: the agent, the
   krun exit code, then the uplink and the test's own services.

`/dinix-boot` must be on the root disk, because dinit is in the store and
the store is not mounted when the kernel looks for `init=`.

## Decisions

### 1. The kernel

No initrd means no module loads before the root is mounted, so the
block device, ext4, virtiofs, overlayfs and the console are built in.

| kernel | backends | for | against |
| --- | --- | --- | --- |
| **libkrunfw's** (6.12) | krun | built for libkrun, cached, everything built in, predates the 6.15 DAX bug our fuse patches fix | its payload API takes no command line: libkrun's default (`rootfstype=virtiofs rw`) roots on virtiofs, not a disk; the kernel lives inside the library, so it cannot go to `load_external`. Needs a libkrun change. No modules, no DAMON, no `NFT_TPROXY`/`NFT_SOCKET` (the relay's UDP) |
| **ours**: nixpkgs' kernel, drivers built in | krun, QEMU | the version NixOS guests run; DAMON, modules from the store after it mounts; one config for both VM backends | a kernel to build and cache (tens of minutes a bump); carries the two fuse DAX patches as kernel patches |
| **UML's** (`umlKernel`) | UML | exists, built in already | UML only |

Recommended: ours for krun and QEMU, UML's for UML. **Decided (User,
2026-11-17): ours.**

### 2. The init on the root disk

| init | for | against |
| --- | --- | --- |
| static busybox and a shell script (UML's `uml-init` shape) | exists in vivarium, quick | a shell at PID 1's door; dinix-init's rule is that its image can exec nothing |
| a small static binary in dinix (User's idea) | one init for every dinix VM; dinix owns its boot | built and reviewed in dinix (pushed outside nixidae) |

**Decided (User, 2026-11-17): `dinix-boot`, a separate static binary in
dinix.** What exists does not reach: `dinix-init` runs as a dinit
service, mounts nothing and by design execs nothing; the container entry
is a `makeBinaryWrapper` that execs dinit after the runtime mounted the
root. The kernel's `init=` runs before `/nix` is mounted, so the first
process is static and lives on the root disk. `dinix-boot` reads a
spec Nix writes (mounts, in order, then the program to exec) and knows
nothing of vivarium; vivarium writes the mounts each backend needs.
`dinix-init` keeps its no-exec rule. `dinix-boot` is in nixidae's
`dinix/`, and the kind reads it through `sources.dinix`.

### 3. Backend contracts

- **krun**: `vivarium.boot.kernel` with no `system`: the kernel's
  `cmdline` names dinix's init, and `vmlinux` is what libkrun loads.
  A NixOS guest writes the same option with its `system`. krun keeps
  owning the store allowlist, DAX, the agent and `/artifacts`.
- **QEMU**: `vivarium.boot.root` is for a foreign OS with no store and
  no agent. A dinix guest wants the NixOS path's store and agent with its
  own kernel: the same `vivarium.boot`, which QEMU reads too.
- **UML**: `vivarium.boot.kernel.image` is the UML kernel, and the kind's
  own root image goes in `vivarium.boot.root`.

### 4. The agent's port, without udev

No udev makes `/dev/virtio-ports/vivarium-agent`. The agent takes
`VIVARIUM_AGENT_DEVICE=virtio-port:vivarium-agent` and finds the port
whose `/sys/class/virtio-ports/*/name` matches, as udev's rule does. No
fixed `vportNpM`: the number follows device order.

### 5. The ready line

The runner waits for the agent's ready line on the console. dinix's
default `dinix.log` is `console` (`shares-console`), and dinit in system
mode writes to `/dev/console`, which is `console=ttyS0`.

### 6. The shell the agent runs

`succeed` runs `/bin/sh -c` with `VIVARIUM_GUEST_PATH` as `PATH`. The
image holds `/bin/sh` as a link into the store, and the kind sets the
path to a profile of the guest's packages.

### 7. The exit code

A dinit service sets `crash` when it starts and `poweroff` when it stops.
It cannot tell a reboot from a poweroff yet, so a reboot reads as a
poweroff: deferred until something reboots a dinix guest.

### 8. The agent's init

**Decided (User, 2026-11-17): dinit-client.** The agent answers
`system_state` (the boot ended: the init's word and what failed) and
`poweroff` for its init; `VIVARIUM_INIT` names it, systemd by default.
On dinit it asks over the control socket with dinix's dinit-client: the
`boot` service started or gave up, waited on through its events, then
every service stopped for a failure. The boot phase and teardown call
these, so a dinix guest boots and powers off as a NixOS guest does. The
systemd-only calls (`unit_state`, `unit_info`, `list_units`, `journal`)
refuse on dinit by name.

## Measured

`checks.dinix`, krun, one guest at the krun defaults on this host
(`--cpus=16 --memory=42762`), 2026-11-17: the kernel's first line at
0.98 s after the runner started, `vivarium-agent: ready` at 2.06 s; about
1 s of it is host setup. Teardown's poweroff takes 1.1 s, most of it
dinit's second between TERM and KILL. Measure again with small `cpus`
and `memory` before quoting a box's cost.

## What is easy to miss

- **Unit queries.** `unit_state`, `unit_info`, `list_units`, `journal`
  and so `wait_for_unit` still ask systemd only. dinit's answers (a
  service's state and stop reason, `catlog`) go behind the same calls.
- **No journal.** The journal events vivarium streams come from
  journald. dinit keeps a log buffer a service can read
  (`dinitctl catlog`); the console carries the rest.
- **The uplink.** The vsock relay and its namespace are NixOS units
  today. Written once as nixpkgs modular services, which dinix
  implements, they run under systemd and dinit alike. The kernel needs
  veth, network namespaces, nftables with tproxy and socket matching.
- **Memory.** Free page reporting needs only the kernel; DAMON reclaim
  needs `CONFIG_DAMON`, which libkrunfw lacks.
- **`switch_to` and `add_closure`** are NixOS's. `add_paths` works:
  the pytest worker's Python reaches a dinix guest that way.
- **Peers and `/etc/hosts`.** A NixOS guest knows its peers by name
  from `/etc/hosts`; a dinix guest needs its setup service to write it.

## Order of work

1. Done: the kernel and the root image, booted under krun.
2. Done: dinit with the agent and the exit code; `succeed` answers and
   poweroff is clean (`checks.dinix`).
3. Half done: the agent's init abstraction. `system_state` and
   `poweroff` answer for dinit; the unit queries do not yet.
4. Half done: the container backend; its own relay under krun and in a
   container. The uplink as modular services; QEMU and UML.
5. The pytest plugin's in-guest tests on a dinix box.
