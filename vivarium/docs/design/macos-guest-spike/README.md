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
| fdba6923 | vivarium | vmapple's `disable-legacy` global is optional |
| 52d6c471 | vivarium | vhost-user's start kick goes through the host notifier |
| 887da5a9 | vivarium | vhost's shared dirty log without memfd |
| 8de4c384 | vivarium | HVF keeps Apple's `ASPSR_EL1` and `AFPCR_EL0` |
| 61560043 | vivarium | vmapple `pci-caps-ascending`: PCI capabilities in ascending order |

`vivarium/pkgs/qemu-vmapple/package.nix` builds the `nixidae` branch of
github.com/nixidae/qemu (61560043). For a local checkout:
`.overrideAttrs { src = builtins.fetchGit { url = ~/Dynamist/qemu; rev = ...; }; }`.

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
| 27 | iBootStage1 (mBoot-20457.41.3) panic `4962211b03d706d:203` | not run | fails in iBoot; with the three fixes below (fork 61560043), 27.2 reaches SSH after about 2 min |

Headless is the working configuration. With `gfx=off` the fork hides the
GPU and IOSurface-mapper nodes in the device tree (one letter of their
`compatible` values), so no GPU driver loads. 14 boots with or without it.

With the GPU on, 15 and 26 panic: `Undefined kernel instruction
instr=d51ef860` = `msr S3_6_C15_C8_3, x0`, an Apple implementation-defined
register. Public HVF hands the write to QEMU, and QEMU without fork 8de4c384
raises UNDEF (below). Headless avoids it at boot. Panic records are in the run
directory's `aux.img` (NVRAM), readable as strings.

With fork 8de4c384 the GPU works on 15 and 26 (2026-11-16): `GFX=on
host/installer_test.py` passes on both, case-insensitive volume, and
`AppleParavirtGPU` attaches in the 15 guest (`ioreg`). `GFX=on try_image.sh`
on 26: SSH after 26 s, 0 resets in 180 s. `GFX` in
`host/darwin_builder.py` sets the machine's `gfx`; the default stays `off`.

Headless needs two Apple registers (2026-11-16). Under `host/installer_test.py`
the kernel of 14 and 15 reads and writes `ASPSR_EL1` (`S3_6_C15_C8_3`) and
`AFPCR_EL0` (`S3_6_C15_C2_5`; names from Asahi's m1n1 `apple_regs.json`)
while it runs ordinary processes. Without 8de4c384 it panics in a 3-page
`true`. The fork keeps one value of each per vCPU. In one full run the guest
wrote each 21345 times, every time 0. With it the installer test passes on 14
and 15, both volume kinds. The values are not in the migration stream.

`installer_test.py` logs QEMU's unhandled-sysreg trace to
`RUN_DIR/qemu-trace.log`, so the next unknown register is named there;
`TRACE_SYSREGS=1` adds every access HVF hands to QEMU.

### macOS 27 (2026-11-16)

Three layers, each found under a breakpoint (TCG, QEMU's gdbstub, a small
GDB-protocol client):

1. The iBoot panic `4962211b03d706d:203` is iBootStage1's synchronous-exception
   handler. The fault is an EL0 task's memcpy into an unmapped error-log
   buffer while it reports error `0x40200003`: the bdif USB device (register
   4 must read `0x1a01`) is absent, and iBoot wants it for its recovery
   prompt. With `-chardev ...,id=vusb -global vmapple-bdif.usbdev=vusb` the
   panic is gone and LLB prints `Entering iBootStage1 recovery mode`.
2. Recovery, because LLB finds no `69646961-6700-11AA-...` (iBoot System
   Container) partition on `root`: the tart image's `config.json` says
   `"diskFormat":"asif"`, and the disk starts with ASIF's `shdw`. QEMU hands
   the ASIF container to the guest as the disk. `diskutil image create from
   --format RAW disk.asif disk.raw` converts it (14, 15 and 26 are raw).
   With the raw disk, iBootStage1 and iBootStage2 run and boot the kernel.
3. The kernel never finds its root volume: `Waiting on ... boot-uuid-media`,
   then `boot-picker-bringup-reason` in NVRAM. No virtio device reaches
   ACKNOWLEDGE (`-trace virtio_set_status`: resets only), and the guest
   never enables memory or bus mastering on one (`-trace pci_cfg_write`;
   26 writes 0x100007 and MSI-X enable). `AppleVirtIO.kext` and
   `AppleVirtIOStorage.kext` load (`kextlog=0xfff`), and their personalities
   match as in 26 (`IOPCIPrimaryMatch 0x00001af4&0x0000FFFF`).
   `-trace pci_cfg_read` shows the cause: the guest reads each virtio
   device's config space about 34000 times a second. Its
   `AppleVirtIOPCITransport::start` loops over the vendor capabilities and
   alternates between 0x84 and 0x70. Its code equals 26's (the kernelcache
   from Preboot, IMG4 with an lzfse payload; `ipsw kernel extract` and
   `ipsw macho disass`). 27's IOPCIFamily is new:
   `IOPCIDevice::extendedFindPCICapability` asks a capability cache first
   (`IOPCIDevice::lookupCapability`). The cache gives the first entry of the
   ID with an offset above the current one, and on a miss the old walk of
   the chain answers. QEMU links each new capability at the head of the
   chain, so the chain runs down (0x98, 0x84, 0x70, 0x60, 0x50, 0x40): from
   0x84 the cache misses and the walk gives 0x70; from 0x70 the cache gives
   0x84. Fork 61560043 adds `-M vmapple,pci-caps-ascending=on`, which
   relinks each device's chain in ascending order at machine init. With it
   every virtio device starts, the kernel mounts `disk3s1`, and 27.2 (build
   26B123) answers SSH with DHCP on `en0`. `PCI_CAPS_ASCENDING=on
   IMG=…/goldengate-raw host/installer_test.py` passes on the package
   build (case-insensitive volume: installed in 27.5 s, checked in 58 s).
   26 also boots with it on. A builder snapshot works too:
   `PCI_CAPS_ASCENDING=on IMG=…/goldengate-raw darwin_builder.py prepare`
   (127 s), `save` (107 s, 6.6 GB), then `build`: daemon after 0.33 s, a
   derivation that writes `sw_vers -productVersion` built in 1.3 s (`27.2`). It is
   off by default, because a snapshot saved with the old chain does not load
   with the new one: QEMU compares the next pointers. `PCI_CAPS_ASCENDING=on`
   sets it in `host/darwin_builder.py`; `MACHINE=vmapple,pci-caps-ascending=on`
   in `host/run-hvf.sh`.

`host/nvram_bootargs.py AUX_IN AUX_OUT ARGS` sets boot-args in an aux.img:
both NVRAM banks (0xA00000, 0xA80000; the higher generation is live), each
with its Adler-32 over +0x14 to the bank's end. `-v serial=3` puts the kernel
log on the serial port.

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

### Disk and RAM size (2026-11-13)

`darwin_builder.py compact IMG_DIR ROOT` converts tart's `disk.raw` to a
zstd-compressed `disk.qcow2`, checks it with `qemu-img compare`, and rebases
every overlay under ROOT onto it with `rebase -u`. The content is identical,
so a saved RAM still matches its disk and no snapshot needs a new save.

| image | raw on disk | zstd qcow2 | convert and compare |
| --- | --- | --- | --- |
| Sonoma | 27 GB | 19 GB | 82 s |
| Sequoia | 30 GB | 22 GB | 97 s |
| Tahoe | 30 GB | 23 GB | 96 s |
| Golden Gate | 36 GB | 28 GB | 119 s |

The guest TRIMs its free space at boot (the Sonoma overlay held 14.7 GiB of
zero clusters), but the APFS container really holds 29.7 GB: System 10.3,
Preboot 6.1, Data 8.9 (Homebrew 1.8, CommandLineTools 2.4), Nix 3.4.
Compression, not free space, is the gain. zstd costs nothing measurable:
`hello` took 26.7–27.6 s on zstd, 27.3–28.3 s on plain qcow2 and 26.4–27.5 s
on raw.

`ram.bin` must stay a file QEMU maps, so a start stays lazy. APFS
transparent compression refuses a file over 2 GiB (`afsctool`,
`CMP_MAX_SUPPORTED_SIZE`). zstd takes the 5.1 GB of nonzero pages to 1.3–1.5
GB, but a start must then decompress before it maps. Instead `save` puts less
in RAM: `guest/zero-free.nix` builds `guest/zerofree.c` in the guest and
touches the free pages (4.9 GiB of 8), which the kernel zero-fills; migration
skips a zero page, so `ram.bin` has holes there.

`WARMUP` builds a Nix file in the guest before the save. A `hello` warm-up
leaves stdenv's `.drv` closure in the guest store: a later `hello` copies 1
`.drv` instead of 553.

| Sonoma snapshot | `ram.bin` on disk | `hello`, start to output on host |
| --- | --- | --- |
| seeded with stdenv | 5.2 GB | 33–35 s |
| seeded, `hello` warm-up, free pages zeroed | 3.4 GB | 28 s |

Both rows were measured while a conversion ran. The same `hello` built on the
host itself took 35 s, so what remains is the compile.

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

## The host store in the guest (2026-11-14)

The aim: a guest that reads the host's store in place, as the Linux builder's
local overlay store does, with no copy. macOS has no overlayfs, but a store is
flat and its paths never change, so an overlay of one directory level is
enough. BSD union mounts give that.

- A filesystem mounted with `-o union` resolves a name its root lacks in the
  directory it covers. Whole trees under that name come from below. Writes go
  to the top. `readdir` lists the top only; `rm` of a lower name acts on the
  lower filesystem. Measured on macOS 14.8.7 and 26.6.2, with tmpfs and APFS
  on top.
- The guest has drivers for virtio-9p (`AppleVirtIO9P`) and virtiofs
  (`AppleVirtIOFS`). QEMU's `virtio-9p-pci` runs in QEMU, on a Linux or macOS
  host, with no helper process and no shared guest RAM. virtiofs needs both.
- `mount_9p` mounts only at `/Volumes/<tag>` and refuses a symlink there.
  `guest/mount9p.c` makes its `mount(2)` call at any directory: type
  `AppleVirtIO9P`, 48 bytes of data (magic `P9VA`, the device's
  `AppleVirtIO9PFSUUID` from the IORegistry, a read-only word at 0x2c).

Measured on macOS 14 with `-fsdev local,path=/nix/store,readonly=on,security_model=none`
and `-device virtio-9p-pci`: the host store at the guest's `/nix/store`, then an
APFS volume union-mounted over it. A host-built `hello` runs from there and
finds its `libiconv` under `/nix/store` (10 runs in 0.29 s). A new path lands
on the APFS volume. A write to the 9p layer fails with `EINVAL`. A 110 MB
file reads in 0.16-0.20 s; walking a whole `llvm` lib output takes 0.55 s.
A listing of the host store root (170 000 entries) did not finish in minutes.

### 9p is too slow for a toolchain (2026-11-14)

clang-wrapper 21.1.8's closure (42 paths, 1.2 GB, 18 442 files) in a macOS 14
guest (8 GiB, 4 CPUs) on this M5 Pro host. Guest-local is the same paths
copied to the guest's APFS store. Warm runs:

| | 9p | guest-local | host |
| --- | --- | --- | --- |
| `cc -c` of a file that includes CoreFoundation, 10 times | 14.4 s | 0.52 s | 0.74 s |
| link it | 1.96 s | 0.10 s | 0.15 s |
| `find` over apple-sdk-14.4 (12 734 files) | 89 s | 0.16 s | 0.08 s |
| `cat` every file of it | 122 s | 0.41 s | 0.35 s |
| `cat` libLLVM.dylib (148 MB) | 0.16 s | 0.02 s | 0.02 s |

Cold runs (`purge` first) are the same as warm ones over 9p: the guest caches
nothing it reads there. Copying the closure out of 9p took 93 s; one `tar`
stream of it, 105 s. Traced with `-trace v9fs_*` (`-msg timestamp=on`):

- QEMU answers `getattr` in a median 133 us, `readdir` in 3.6 ms.
- Between a reply and the next request, the median gap is 149 us, but one in
  ten is 7.6 ms or more: the wait is in the guest.
- The guest reads about 11 entries of each 9 KB `readdir` reply and asks again
  from the next offset. A Darwin host resumes with `seekdir`, so a listing
  costs about the square of its size.
- The guest sends about 2 `readlink` requests for each entry it looks up.

### virtiofs: Apple's server, measured through tart (2026-11-14)

QEMU can serve virtiofs only through vhost-user. meson turns vhost-user off
on hosts other than Linux (`meson.build:223`; `-Dvhost_user=enabled` allows
it, and `memory-backend-shm` exists for it). The server is the blocker:
virtiofsd keeps every inode as an `O_PATH` descriptor reopened through
`/proc/self/fd`, and uses `statx`, `name_to_handle_at`, namespaces, seccomp
and capng. Its `vhost-user-backend` 0.22 runs on `epoll`. A vhost-user
server also needs the guest RAM shared, where a start maps `ram.bin` private.

Virtualization.framework has its own virtiofs server. The same benchmark in a
clone of `sonoma-vz` (tart 2.37.0, `--dir=/nix/store:ro,tag=hoststore`,
`mount_virtiofs`, 4 CPUs, 8 GiB), warm runs. Guest-local here is the VZ
guest's own disk, which is slower than the QEMU guest's:

| | virtiofs | guest-local (VZ) | 9p (QEMU) |
| --- | --- | --- | --- |
| `cc -c`, 10 times | 2.04 s | 1.33 s | 14.4 s |
| link | 0.36 s | 0.29 s | 1.96 s |
| `find` over the SDK | 4.68 s | 0.38 s | 89 s |
| `cat` every SDK file | 7.92 s | 0.74 s | 122 s |
| `cat` libLLVM.dylib | 0.05 s | 0.04 s | 0.16 s |
| copy the closure out | 20.6 s | | 93 s |

Warm runs are faster than cold ones (a compile: 0.87 s cold, 0.20 s warm),
so the guest caches what it reads over virtiofs. A listing of the store root
still did not finish in 120 s.

### virtiofs under QEMU: darwin-virtiofsd (2026-11-14)

`vivarium/pkgs/darwin-virtiofsd` is a vhost-user virtio-fs server for macOS
hosts: its own vhost-user side, libkrun's FUSE server and macOS passthrough,
read-only, `CachePolicy::Always` with 24 h entry and attribute timeouts. QEMU
needs `--enable-vhost-user --disable-vhost-net` and the fork's 52d6c471, and
guest RAM on `memory-backend-shm`:

```
darwin-virtiofsd --socket-path vfs.sock --shared-dir /nix/store &
qemu-system-aarch64 ... -object memory-backend-shm,id=ram0,size=8G \
  -chardev socket,id=vfs,path=vfs.sock -device vhost-user-fs-pci,chardev=vfs,tag=hoststore
# guest
mount_virtiofs hoststore /nix/store
```

Found on the way:

- vmapple's `virtio-pci.disable-legacy` global refused the modern-only
  `vhost-user-fs-pci`; the fork makes it optional.
- vhost-user writes its start kick to the kick fd, which on a host without
  eventfd is the read end of a pipe (`EBADF`); the fork sends it through
  `event_notifier_set`.
- The guest's driver puts every request in an indirect descriptor table.
  QEMU passes `VIRTIO_RING_F_INDIRECT_DESC` to the guest whatever the
  back-end offers, and libkrun's queue had no indirect support.
- The guest uses one queue, the first.

The same benchmark in the QEMU guest (macOS 14, 4 CPUs, 8 GiB), warm runs;
the Apple column is the tart run above:

| | darwin-virtiofsd | Apple's server (VZ) | 9p | guest-local |
| --- | --- | --- | --- | --- |
| `cc -c`, 10 times | 1.81 s | 2.04 s | 14.4 s | 0.47 s |
| link | 0.28 s | 0.36 s | 1.96 s | 0.09 s |
| `find` over the SDK | 5.82 s | 4.68 s | 89 s | 0.15 s |
| `cat` every SDK file | 10.1 s | 7.92 s | 122 s | 0.38 s |
| `cat` libLLVM.dylib | 0.03 s | 0.05 s | 0.16 s | 0.02 s |
| copy the closure out | 31.4 s | 20.6 s | 93 s | |
| list the store root (170 510) | 32.6 s | over 120 s | over 120 s | |

### Snapshots with virtiofs (2026-11-14)

vhost-user needs guest RAM the server can map: shared memory, where a start
maps `ram.bin` private. QEMU saves a `vhost-user-fs-pci` guest only when the
server offers two features, which darwin-virtiofsd now has:

- `LOG_SHMFD` and `VHOST_F_LOG_ALL`: without them QEMU blocks migration. The
  server marks each finished chain's writable buffers and the used ring in
  QEMU's bitmap. A stopped VM's device is stopped, so a save logs nothing;
  QEMU allocates the bitmap with memfd, which macOS lacks, so live
  migration is still untested.
- `DEVICE_STATE` (`SET_DEVICE_STATE_FD`, `CHECK_DEVICE_STATE`): the state is
  that no FUSE session is open. Between FUSE_INIT and FUSE_DESTROY the guest
  holds inodes only that process knows, so the server refuses the save: save
  with the share unmounted, mount it after each start.

`host/snap_vfs.py`: Sonoma, 8 GiB, saved with the share unmounted 20 s after
the daemon answered. `full.bin` 3.4 GB; the split to `ram.bin` + `state.bin`
8.5 s. Each start mounts the share, then compiles `host/bench.c` 10 times,
three rounds:

| start | daemon answers | compile x10, rounds 1 / 2 / 3 |
| --- | --- | --- |
| lazy: a clone of `ram.bin`, `memory-backend-file,share=on` | 0.36-0.44 s | 16-18 / 29 / 30-31 s |
| eager: `full.bin` into `memory-backend-shm` | 1.60-1.74 s | 2.3 / 2.0-2.1 / 2.0-2.1 s |

A shared file mapping stalls the guest while macOS writes its dirty pages
back, as on the first builders (Traps). The eager start pays the whole RAM,
1.5 s of loading for 3.4 GB, and then builds as fast as a booted guest.

Two things shorten the eager load (`ZEROFREE=1`, `MULTIFD_CHANNELS=N`):
`guest/zerofree.c` before the save, and QEMU's `mapped-ram` format, which
leaves zero pages out of the file and loads with N multifd threads. The load
must use the save's channel count, so the snapshot records it. Each row is
one save and two starts; compiles stay at 1.7-2.4 s for ten:

| eager start | `full.bin` | RAM loaded | daemon answers |
| --- | --- | --- | --- |
| stream | 3.4 GB | 1.5-1.6 s | 1.60-1.74 s |
| zero-free, stream | 1.5 GB | 1.0-1.2 s | 1.21-1.47 s |
| mapped-ram, 4 channels | 3.9 GB | 1.1-1.5 s | 1.14-1.50 s |
| zero-free, mapped-ram, 4 channels | 1.9 GB | 0.26 s | 0.57-0.61 s |
| zero-free, mapped-ram, 8 channels | 2.0 GB | 0.29-0.30 s | 0.34-0.35 s |

The last row starts as fast as the builders without virtiofs (0.17-0.38 s).
After a mapped-ram start without zero-free, the guest clock stepped back
during the first compile round: those two rounds read negative.

QEMU allocated the shared dirty log with memfd, so on macOS a running guest
with vhost-user could not migrate; the fork's 887da5a9 falls back to
`qemu_shm_alloc`. A live save (`LIVE=1`, zero-free, 8 channels) then
completes in 21.7 s, and a start from it builds at full speed, but its
daemon answered only after 21.3 s, both times. Open; builders save a stopped
guest.

### A builder whose store lies over the host's (2026-11-14)

`VIRTIOFS=1` makes `save` and every start use the host store:

- `prepare` moves the guest's store paths onto a new APFS volume, "Nix
  Upper", union-mounted on `/nix/store`, with an fstab `noauto` line (else
  diskarbitration mounts it under /Volumes at boot and the union mount gets
  `EBUSY`). It turns off the installer's nix-daemon, which would hold the
  volume; the builder's daemon is vsock's `nix-daemon --stdio`.
- `guest/builder-store.sh` serves vsock port 3032 as root: `host` unmounts
  the volume, mounts the share with `mount_virtiofs`, and mounts the volume
  over it again; `load-db` runs `nix-store --load-db`; `zerofree`; `status`.
- `save` boots with the share unmounted, checks the union, zeroes free pages
  and saves mapped-ram with 8 channels: `state.bin` is the whole snapshot RAM.
- A start runs darwin-virtiofsd on the host store and calls `host`. `build`
  evaluates the derivation, then registers its closure with the outputs the
  host has (`nix-store --query --requisites --include-outputs`,
  `--dump-db`; pynix has neither) through `load-db`. The guest copies none
  of those paths; it reads them over virtiofs.

Sonoma, prepared from `sonoma-nix` with no stdenv in the guest:

| | |
| --- | --- |
| prepare (971 store entries moved) | 72 s |
| save | 67 s, `state.bin` 1.7 GB |
| RAM loaded, `state.bin` in the host's cache | 0.26 s |
| RAM loaded, not in the host's cache | 1.1 s |
| daemon answers | 0.61 s (1.44 s uncached) |
| host store mounted under the guest's | 0.6 s more |
| registering `hello`'s closure, 934 paths | 1.4-1.7 s |
| `hello`, start to output on the host | 43.3-44.1 s |

The `hello` build copied 0 paths to the guest and substituted none: the
unseeded snapshot of 2026-11-08 substituted 60 and took 44 s, the seeded,
warmed one 28 s. The toolchain is read cold over virtiofs: a save unmounts
the share, so the guest's cache of it never reaches a snapshot.

Both together: `SEED` (stdenv) and `WARMUP` (a `hello`) put the toolchain on
the Nix Upper volume and its pages in the saved RAM; everything else comes
from the host store. `state.bin` 1.9 GB, save 102 s. Two starts:

| | run 1 | run 2 |
| --- | --- | --- |
| RAM loaded | 0.29 s | 1.21 s (not in the host's cache) |
| daemon answers | 0.36 s | 1.28 s |
| host store mounted | 0.73 s | 1.65 s |
| 934 paths registered | 2.14 s | 2.72 s |
| `hello`, start to output | 23.4 s | 23.7 s |

0 paths copied, 0 substituted: faster than the warmed snapshot without
virtiofs (28 s) and than the host building `hello` itself (35 s).

`pynix build --darwin-builder` takes such a snapshot as it is: it starts the
server `snapshot.json` names (`virtiofs.server`), loads mapped-ram, calls
`host`, and registers the closure between evaluation and build with
huggorm's `compute_fs_closure(include_outputs=True)` and
`make_validity_registration`. 934 paths took 0.30 s that way, against
1.4 s through `nix-store`; `hello` took 25.4 s from command to output path.

With the device node of Nix Upper looked up once a boot (`diskutil info`
took 0.1 s a call; `host`'s three mounts take 0.07 s together), three
`pynix build --darwin-builder` runs of `hello` took 23.1-23.4 s, registering
in 0.27-0.50 s. Ready, the host store mounted, in 0.52 s with `state.bin` in
the host's cache and 1.44-1.47 s without. The cache did not hold from one run
to the next: on this 24 GiB host each run adds 8 GiB of shared guest RAM.
Read just before, a start was ready in 0.79 s. The guest uses about 2 GB;
less guest RAM is the next thing to try.

### A build's store in the guest (2026-11-15)

pynixd's darwin build VM needs what its Linux one has: the store filtered to
the build's inputs, read-only, and a place where the outputs land on the host.
darwin-virtiofsd takes the libkrun fork's allowlist (`--allowlist FILE`) and a
writable mode (`--writable`, `PermissionSemantics::LinuxSimplified`).
`host/probe_union.py` boots a prepared guest with two shares and checks each
step, on macOS 14:

- The filtered store share shows an allowed path and hides the others, in a
  lookup and in a listing.
- **A virtiofs share as the upper layer of a union hides the lower layer**:
  after `mount -t virtiofs -o union`, the allowed path was gone. So the
  outputs cannot land on the host through a union.
- `mount_tmpfs -o union` over the filtered share works: the inputs show
  through, a write into one or a removal of one fails with `EROFS`, and a new
  output lands on the tmpfs. After the build the guest copies each output into
  a writable share.
- On the writable share, a create, a `chmod` and a `mkdir` keep the guest's
  modes on the host. With the default `LinuxComplete` the host file was 0600
  or 0700 and the guest's mode went into an xattr.
- **macOS `cp -Rp` (copyfile) turned a 0644 file into 0755** on the share; a
  plain create and `chmod` did not. The guest copies outputs without it.

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

1. Builders with less guest RAM: the guest uses about 2 GB, and each 8 GiB
   start pushes `state.bin` out of this 24 GiB host's file cache.
2. Make the snapshot a thing pynix can find, as `withGuest` does for the Linux
   builder: a default snapshot path, and `darwin_builder.py` moved into
   nanopynix (`pynix darwin-builder prepare|save`). The tart image and the
   snapshot are machine state, not store paths: tens of GB for each macOS
   version, and the snapshot names the image's absolute path.
3. vivarium: a darwin host. The runner assumes Linux (passt, crun, UML,
   `-accel kvm`). A macOS guest needs slirp or vmnet networking, HVF, the
   vmapple arguments from `qemu_args`, and an agent without the systemd and
   `/proc` calls. The agent already runs (above).
4. macOS 27 from tart: convert its ASIF disk to raw in `prepare`, so the
   image works without the manual `diskutil` step (above).
5. Find what QEMU lacks for `AppleVPKeyStore` selectors 17 and 35.
6. A macOS 26 `save` right after `prepare` timed out before the daemon
   answered, three times in about ten runs, and passed when run again. The
   failing runs logged no daemon event, so it stopped while connecting to QMP
   or on the first handshake; the traceback was filtered away each time.
