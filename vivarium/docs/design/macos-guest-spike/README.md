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

## The patch (`orchard-hvf.patch`)

Applies to github.com/yaelliethy/orchard at c73a2bda (QEMU 11.1.50 tree).
The local clone is `~/Dynamist/orchard`, change "spike: vmapple boots macOS 14
under HVF" (not pushed: third-party repository).

- `hw/vmapple/hvc.c`: at the boot CPU's first SET_INITIAL_STATE, rewrite every
  `hvc #0` word in RAM to `hvc #1`; a 10 ms vCPU kick timer under HVF.
- `target/arm/hvf/hvf.c`: HVC exits go to `arm_smccc_dispatch` first; a kicked
  vCPU spinning after a swallowed call gets the call served in place; ISV=0
  data aborts go through orchard's `arm_emulate_ldst`; unknown HVC IDs log once.
- `hw/vmapple/vmapple.c`: an unimplemented device at 0x30240000 (Sonoma's
  iBoot reads it; TCG aborts without it); `VMAPPLE_GPIO_PULLUPS` test switch;
  the reims-vgpu include dropped.
- Kconfig: no reims-vgpu (Rust, Vulkan); `imply MAC_PVG_MMIO`.

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

1. vivarium: a darwin QEMU backend (`accel=hvf`, the `vmapple` machine,
   disk-boot like `Appliance`) and an agent channel. No virtio-console or
   virtio-socket on this machine yet: SSH, or add virtio-vsock to vmapple.
2. Carry the QEMU patch somewhere buildable by Nix (an overlay over nixpkgs'
   QEMU, or a nixidae fork of orchard), with the hand-made signing step.
3. Fix apple-gfx-mmio on a macOS 27 host (`setMapMemory:` is gone; Mohamed
   Mediouni's vmapple series on qemu-devel fixes it).
4. Find what QEMU lacks for `AppleVPKeyStore` selectors 17 and 35.
