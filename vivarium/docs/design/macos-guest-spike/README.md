# Spike: aarch64-darwin guests on QEMU + HVF

Off-main spike for nixidae/nixidae#1168. Host: MacBook M5 Pro, macOS 27.0.1, SIP on.

## State (2026-11-06)

- macOS 14.8.7 (23J520, tart `cirruslabs/macos-sonoma-base`) boots on QEMU's
  `vmapple` machine under HVF. SSH answers about 14 s after boot. Commands run.
  The guest reaches the internet (`cache.nixos.org` returns 200).
- The guest stays up once tart's guest daemon is off:
  `sudo launchctl disable system/org.cirruslabs.tart-guest-daemon`. With it on,
  the guest called `reboot()` about every 22 s (141 resets in a row; none in
  the 3 minutes after the disable). The daemon wants tart's host side over
  virtio-socket, which this machine does not have. Under tart it stays up.

## Why QEMU needs a patch

- Public Hypervisor.framework answers `hvc #0` with an Apple CPU service ID
  (`0xc1……`) or `0xc6……` in the host kernel with 0xffffffff. It never exits.
  macOS 13+ spins on `cbnz x0, .` after the PAC call. `probes/hvc.c` shows it.
- `hvc #1` and `smc` with the same ID exit to user space (`probes/hvc2.c`).
- Only ISA mode 1 creates a VM without `com.apple.private.hypervisor`
  (`probes/isa.c`). Virtualization.framework uses modes 4/1/3.

## The QEMU fork

github.com/nixidae/qemu, branch `nixidae` (local clone `~/Dynamist/qemu`):

| commit | author | what |
| --- | --- | --- |
| f8aef8a9 | upstream | base: the commit orchard's vendored tree was copied from |
| 4127264f | Youssef (orchard) | orchard import: vmapple, reims-vgpu, PAC |
| 962b4b02 | Youssef (orchard) | KVM apple-vm fixes |
| bc048138 | Youssef (orchard) | Apple hypercall contract; KVM/WHPX paths |
| bb6b12b5 | Youssef (orchard) | TCG performance and input fixes |
| 601f3c10 | vivarium | virtio-vsock-unix |
| 3727918b | vivarium | vmapple boots macOS 14 under HVF |
| e72420f7 | Mohamed Mediouni (qemu-devel v8 05) | apple-gfx on macOS 15.4+ hosts |
| a8a6e107 | Mohamed Mediouni (qemu-devel v8 10) | virtio-blk: FileVault off |
| 2a4a3122 | vivarium | hide the paravirtualized GPU when `gfx=off` |
| 24f1c3b0 | vivarium | virtio-vsock-unix migrates; transport reset on load |

24f1c3b0 is local: the `nixidae` bookmark on github.com/nixidae/qemu is at
2a4a3122 until it is pushed. `vivarium/pkgs/qemu-vmapple/package.nix` builds
the fork with Nix; its default source names 24f1c3b0 with a fake hash, so for
now pass `src = builtins.fetchGit { url = ~/Dynamist/qemu; rev = ...; }`.

`host/find_qemu_base.py` found the base by content hashes: orchard's import
differs from f8aef8a9 only in files orchard changed or added.
`host/replay_orchard.sh` builds the branch; its tree equals orchard's
(github.com/yaelliethy/orchard c73a2bda plus the two vivarium changes) file
for file, plus the upstream files orchard dropped (Rust `.wrap` files,
`tests/keys`, submodule pointers).

The same two vivarium changes as patches against orchard c73a2bda:

1. `orchard-vsock.patch`, "virtio-vsock-unix": the vsock device below.
2. `orchard-hvf.patch`, "spike: vmapple boots macOS 14 under HVF":

- `hw/vmapple/hvc.c`: at the boot CPU's first SET_INITIAL_STATE, rewrite every
  `hvc #0` word in RAM to `hvc #1`; a 10 ms vCPU kick timer under HVF.
- `target/arm/hvf/hvf.c`: HVC exits go to `arm_smccc_dispatch` first; a kicked
  vCPU spinning after a swallowed call gets the call served in place; ISV=0
  data aborts go through orchard's `arm_emulate_ldst`; unknown HVC IDs log once.
- `hw/vmapple/vmapple.c`: an unimplemented device at 0x30240000 (Sonoma's
  iBoot reads it; TCG aborts without it); `VMAPPLE_GPIO_PULLUPS` test switch;
  the reims-vgpu include dropped.
- Kconfig: no reims-vgpu (Rust, Vulkan); `imply MAC_PVG_MMIO`.

## Agent channels

`host/run-hvf.sh` gives the guest both.

**virtio-serial** (QEMU's own `virtio-serial-pci` and `virtserialport`, no
patch). macOS binds `AppleVirtIOConsole`; the port named `vivarium-agent` is
`/dev/cu.vivarium-agent` in the guest and `$RUN/agent.sock` on the host. Data
passes both ways (`host/serial_roundtrip.py`). This is the channel vivarium's
Linux QEMU guests use.

**virtio-vsock** (`virtio-vsock-unix-pci`, new in `orchard-vsock.patch`).
QEMU has no userspace vsock device and vhost needs Linux, so this one is in
QEMU's process. macOS binds `AppleVirtIOSocket`; guest CID 3. The host side
follows Firecracker: host to guest, connect to `$RUN/vsock.sock` and write
`CONNECT <port>\n`, read `OK <host port>\n`; guest to host, CID 2 port P
reaches the unix socket `$RUN/vsock.sock_P`. `VSOCK_UNIX_DEBUG=1` traces
each packet to stderr.

| test (`guest/vsockcat.c`, `host/vsock_connect.py`, `host/unix_echo.py`) | result |
| --- | --- |
| host to guest, 20 echo round trips | 20/20 |
| host to guest, 64 MiB echoed | 187 MB/s each way, every byte back |
| guest to host, 64 MiB echoed | 121 MB/s each way, every byte back |
| host to a guest port with no listener | handshake fails (RST), no OK |

`vsockcat` builds on the host: `clang -O2 -mmacosx-version-min=14.0 -o
vsockcat guest/vsockcat.c`; the configured guest has it in `/usr/local/bin`.
`vsockcat serve PORT CMD...` is inetd: each connection runs CMD on stdin and stdout.

## Rebuild and run

```sh
cd ~/Dynamist/orchard/qemu
nix develop --file /etc/nixpkgs qemu --command sh -c 'mkdir -p build-hvf && cd build-hvf && \
  ../configure --target-list=aarch64-softmmu --enable-hvf --disable-docs --enable-slirp \
  --disable-werror --disable-rust && ninja qemu-system-aarch64-unsigned qemu-img'
# entitlement.sh needs Rez, which the dev shell lacks: sign by hand.
cd build-hvf && cp -pPf qemu-system-aarch64-unsigned qemu-system-aarch64 && \
  /usr/bin/codesign --entitlements ../accel/hvf/entitlements.plist --force -s - qemu-system-aarch64
# The configured guest: Nix installed, tart's daemon disabled.
cd ~/Dynamist/orchard-images && RUN=$PWD/sonoma-nix ./run-hvf.sh
# A fresh guest from the tart image (disable the tart daemon first thing):
cd ~/Dynamist/orchard-images && rm -rf run && mkdir run && cp aux-verbose.img run/aux.img && ./run-hvf.sh
```

- `pynix develop` fails on macOS bash 3.2 (nixidae/nixidae#191), so `nix develop`.
- `~/Dynamist/orchard-images/`: `sonoma/` holds the image (47 GiB sparse, about
  30 GB used), fetched by orchard's `scripts/fetch-tart-image.py` (its final
  `os.fdatasync` fails on macOS after every chunk is written; harmless).
- `run-hvf.sh` environment: `IMG`, `RUN`, `ROM`, `RAM`, `CPUS`, `MACHINE`,
  `ACCEL`, `CPU`, `GFX`. Default: `vmapple`, `hvf`, stock host AVPBooter, `gfx=off`.
- SSH: `ssh -p 2222 admin@127.0.0.1`, password `admin`. QMP: `run/qmp.sock`
  (`host/qmp.py`, `host/qmp_events.py`).
- `~/.tart/vms/sonoma-vz` is the same image for tart (APFS clone of the disk,
  raw NVRAM from `host/fetch_tart_meta.py`); `tart run --no-graphics sonoma-vz`.

## macOS 15, 26, 27 (2026-11-06)

Images: tart `macos-sequoia-base` (15.7.7), `macos-tahoe-base` (26.6.2),
`macos-golden-gate-base` (27), in `~/Dynamist/orchard-images/{sequoia,tahoe,goldengate}`.
Test: `host/try_image.sh IMAGE_DIR RUN_DIR` (fresh overlay, disables the
tart daemon, counts resets); `GFX=on` uses apple-gfx-mmio.

| image | without the v8 patches | patches 05 and 10, `GFX=on` | fork 2a4a3122, no GPU (`gfx=off`, default) |
| --- | --- | --- | --- |
| 15 | panic in AppleParavirtGPUIOGPUFamily on the gfx stub (NULL, far 0x10) | SSH after 41 s; then panic | **SSH after 48 s, 0 resets in 180 s** |
| 26 | panic `unencrypted data volume is not allowed` (apfs_vfsops.c:2372) | SSH reached; then panic | **SSH after 25 s, 0 resets in 180 s** |
| 27 | iBootStage1 (mBoot-20457.41.3) panic `4962211b03d706d:203` | not run | not run (fails in iBoot) |

Headless is the working configuration. With `gfx=off` the fork hides the
GPU and IOSurface-mapper nodes in the device tree (one letter of their
`compatible` values), so no GPU driver loads. 14 boots with or without it.

With the GPU on, 15 and 26 panic: `Undefined kernel instruction
instr=d51ef860` = `msr S3_6_C15_C8_3, x0`, an Apple implementation-defined
register that public HVF (ISA mode 1) refuses. Headless avoids it. Panic
records are in the run directory's `aux.img` (NVRAM), readable as strings.

The 27 panic: LLB takes a data abort from EL0, ESR 0x92000046, writing
`0xfffffc01100bc058` in its memcpy (ELR 0x700adf34) while formatting a log
line; the address does not change with RAM size (4, 8, 16 GiB). No unknown
hypercall and no unimplemented MMIO before it (traced under TCG with
`-d trace:arm_psci_call,int`; lldb on QEMU's gdbstub caught the copy).

## On-demand builders from a snapshot (2026-11-08)

`pynix build --darwin-builder --darwin-builder-snapshot SNAP_DIR` builds in a
macOS VM that the command starts from a saved guest and stops when it exits,
as `--linux-builder` does with a Linux VM. `host/darwin_builder.py` makes the
guest and the snapshot:

```sh
export QEMU=…/qemu-vmapple/bin/qemu-system-aarch64 QEMU_IMG=…/bin/qemu-img
host/darwin_builder.py prepare tahoe guest-tahoe          # tart image -> builder guest, 80 s
SEED=$(stdenv path) host/darwin_builder.py save guest-tahoe snap-tahoe
pynix build --darwin-builder --darwin-builder-snapshot snap-tahoe --file hello.nix
```

- **prepare** (over SSH, tart's admin/admin): disable tart's guest daemon, wait
  for the Nix Store volume, install Nix if absent, install `vsockcat` and two
  LaunchDaemons: `guest/builder-daemon.plist` runs `vsockcat serve 3030
  nix-daemon --stdio` (a root daemon for each vsock connection) and
  `guest/builder-clock.plist` takes the host's epoch on port 3031. With
  `AGENT`, also vivarium's guest agent (below). It runs again on a prepared
  guest and replaces those parts.
- **save**: boot on anonymous RAM, wait until the daemon answers the worker
  handshake over vsock, copy the `SEED` closures in, settle 30 s, `migrate` to
  `full.bin`. A paused QEMU then loads `full.bin` into a shared `ram.bin` and
  writes the device state (`state.bin`, 0.8 MB) with `x-ignore-shared`.
  `snapshot.json` names QEMU, its arguments with `{run}` for the run
  directory and `{snapshot}` for its own, the read-only overlay and the
  vsock ports; a snapshot directory can move. Before the save the clock port
  must report an address on en0, or there is no save.
- **start** (pynix `_impl/darwin_builder.py`): map `ram.bin` private
  (`share=off,readonly=on,rom=off`), so pages fault in on first touch and the
  file never changes; a new qcow2 overlay over the saved one; `-incoming`,
  `cont`; wait for the daemon; set the clock (the guest also renews its DHCP
  lease if en0 has no address, and reports it); serve the daemon port on a Unix
  socket in a 0700 directory. The store URI carries `local-fs=false`: stock
  Nix reads a `unix://` store's paths from the host's own /nix/store.

Measured on the M5 Pro, the Nix-built QEMU, seeded snapshots:

| | macOS 14.8.7 | macOS 15.7.7 | macOS 26.6.2 |
| --- | --- | --- | --- |
| prepare from the tart image | (made by hand) | 79 s | 83 s |
| save: cold boot until the daemon answers | 21 s | 24 s | 24 s |
| start until the daemon answers | 0.17–0.38 s | 0.17–0.2 s | 0.17–0.24 s |
| `pynix build --darwin-builder`, trivial derivation, warm file cache | 0.77–0.83 s | 0.65–0.70 s | 0.79–0.83 s |
| the same, first run after the save (cold file cache) | 2.0 s | 1.5 s | 1.2 s |
| `hello` from source with stdenv, built and copied back | 26 s | 28 s | 29 s |

`pynix build` with no builder, evaluating the same file, takes 0.96 s. Two
builders from one snapshot run side by side. A stdenv-seeded snapshot
substitutes no path for `hello`; an unseeded one substituted 60 and took
44 s. Snapshot size on disk: `ram.bin` 3.4–5.6 GB of 8 GiB, overlay 2.5–3.9
GB over the 26–30 GB tart image, all shared by every start.

`host/vsock_stress.py` against a running builder: 64 concurrent `nix
path-info --all` clients get one answer; a 590 MB closure goes in at
97–122 MB/s with matching NAR hashes; a guest reset with 16 connections open
ends all 16 and QEMU keeps running. The copy rate is the guest store's
hashing and writing: the guest posts 8 KiB rx buffers, and 256 KiB reads in
the device changed nothing (117–119 MB/s), so that change was dropped.
`nix-daemon --stdio` as root serves the operations itself; it does not take
Nix's `select()` proxy (`forwardStdioConnection`), which reads 8 KiB per round
on darwin.

### Traps found on the way

- **A guest on a MAP_SHARED RAM file stalls** for 5–25 s at a time while the
  host pager writes its pages back: SSH failed 8 of 20 probes after a restore,
  and a cold boot reached SSH after 83.6 s instead of 19.8 s. Every running
  guest is on anonymous or private memory now; only the paused converter
  writes the shared file.
- **The Nix Store volume mounts up to 30 s after sshd starts.** An install
  over the unmounted volume fails, and on macOS 26 its cleanup removed
  `/etc/synthetic.conf`, so `/nix` never came back. `prepare` waits while a
  Nix Store volume exists, and never installs over one.
- **launchd and a failing first start:** the agent's first start fails (no
  port device yet); launchd then left the job "spawn scheduled" for over a
  minute despite `KeepAlive`. The jobs wait in a shell loop instead.
  `launchctl kickstart -k` restarts the loaded definition, so a changed plist
  needs `bootout` and `bootstrap` (retried: `bootout` returns early).
- **One vsock request to a fresh macOS 26 guest got no answer and no reset**,
  and the wait for the daemon spent its 30 s on it. It did not happen again.
  Every handshake now times out after 2 s and is retried.
- **A macOS 15 guest was saved with no DHCP lease on en0.** Every build from
  that snapshot failed on `Could not resolve host: cache.nixos.org`; a build
  that ran `scutil --dns` and `ifconfig` showed no address and no route.
  Seeding goes over vsock, so nothing had needed the network. The clock
  service renews the lease at every start, and `save` refuses a guest with no
  address.
- **beartype cannot check anyio's `TaskStatus`** (a Protocol that is not
  runtime-checkable). Its warning reached every pynix command's stderr and
  failed 13 tests; the forwarder carries `@no_runtime_type_check`.
- **About 150 vsock connections in flight at once** (each closed before its
  `nix-daemon` started) make macOS refuse more until they are gone; 300
  sequential full handshakes pass. `vsockcat serve` listened with a backlog of
  16; it uses `SOMAXCONN` now.

### vivarium's guest agent in macOS

`vivarium-runner` built for aarch64-darwin (its `meta.platforms` is Linux
only; the build passes with the check phase off) runs unchanged: the agent
opens `/dev/cu.vivarium-agent`, sets it raw and serves arpyc. From the host
(`host/agent_probe.py`) it ran commands as root, returned exit codes and
answered a round trip in 2.3 ms. `prepare` with `AGENT` installs it as
`guest/vivarium-agent.plist`, rooted in the profile
`/nix/var/nix/profiles/vivarium-agent`. `systemctl`, `/proc` and journal
calls do not exist on macOS.

## Nix in the guest

Official installer (`--daemon`): Nix 2.35.4, sandbox off (installer default).

| step | time |
| --- | --- |
| raw `derivation` with `/bin/sh`, built locally | 0.7 s |
| `nixpkgs#hello` from cache.nixos.org | 11 s |
| `nixpkgs#hello --rebuild`, compiled in the guest with stdenv | 32 s |

`--rebuild` then reports the output differs from the cached one: a determinism
check, not a build failure. `hello` runs. A guest `shutdown -h` powers QEMU
off (PSCI SYSTEM_OFF).

## The reset loop (solved)

The guest called `reboot()` about every 22 s: QMP showed PSCI SYSTEM_RESET,
launchd logged `System shutdown initiated by kernel`. The caller was tart's
root daemon `org.cirruslabs.tart-guest-daemon`; it wants tart's host over
virtio-socket. Ruled out on the way: hypercalls, GPIO power-key polarity,
WindowServer, the NVRAM install phase, the timebase.

Differences from the VZ guest that do not stop a boot, and may matter later:
VZ has `avp-rtc` where QEMU has PL031, and virtio console, entropy, socket,
sound and Apple PCI devices (`106b:1a0x`). `AppleVPKeyStore` logs
`ret: e00002f0` (kIOReturnUnsupported). The Data volume key sits in the
virtual platform keystore, so the host cannot mount it.

`host/collect.py` with `guest/diag.sh` (LaunchDaemon `guest/diag.plist`)
streams guest logs to the host; `log stream` under it stopped within a second
of starting, so it needs more work before it is useful.

## Next steps

1. Push github.com/nixidae/qemu `nixidae` (24f1c3b0), then set the real hash
   in `vivarium/pkgs/qemu-vmapple/package.nix`. Until then the package needs a
   local `src`.
2. Make the snapshot a thing pynix can find, as `withGuest` does for the Linux
   builder: a default snapshot path, and `darwin_builder.py` moved into
   nanopynix (`pynix darwin-builder prepare|save`). The tart image and the
   snapshot are machine state, not store paths: tens of GB for each macOS
   version, and the snapshot names the image's absolute path.
3. vivarium: a darwin host. The runner assumes Linux (passt, crun, UML,
   `-accel kvm`). A macOS guest needs slirp or vmnet networking, HVF, the
   vmapple arguments from `qemu_args`, and an agent without the systemd and
   `/proc` calls. The agent already runs (above).
4. GPU mode: handle `msr S3_6_C15_C8_3` (d51ef860). macOS 27: find what LLB's
   log buffer address comes from.
5. Find what QEMU lacks for `AppleVPKeyStore` selectors 17 and 35.
