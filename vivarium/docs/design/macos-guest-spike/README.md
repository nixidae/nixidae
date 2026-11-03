# Spike: aarch64-darwin guests on QEMU + HVF

Off-main spike for nixidae/nixidae#1168. Host: MacBook M5 Pro, macOS 27.0.1, SIP on.

## State (2026-11-03)

- macOS 14.8.7 (23J520, tart `cirruslabs/macos-sonoma-base`) boots on QEMU's
  `vmapple` machine under HVF. SSH answers about 14 s after boot. Commands run.
  The guest reaches the internet (`cache.nixos.org` returns 200).
- Open blocker: the guest resets itself about every 22 s. The same image under
  Virtualization.framework (tart) stays up, so the cause is QEMU's machine model.

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

## What the reset loop is not

- Not hypercalls: every `hvc #0` is rewritten; the only unknown call is
  `0x8300ff01` (SMCCC Call UID, OEM range).
- Not the power key: pulling GPIO pin 3 up changed nothing.
- Not WindowServer: disabling it and loginwindow changed nothing.
- Not the half-finished update in NVRAM (`IASCurrentInstallPhase`): deleting it
  changed nothing (two variables unconfirmed).
- Not timebase: host counter is 24 MHz.

## What it is, so far

- QMP shows `RESET {"guest": true, "reason": "guest-reset"}`: PSCI SYSTEM_RESET.
- launchd logs `System shutdown initiated by kernel - launchd got a SIGTERM`:
  some process called `reboot()` (`bsd/kern/kern_xxx.c`; it logs no caller).
- Within one kernel boot, userspace reboots too (`KextLog: was reset after
  userspace reboot`).
- Device diff against the VZ guest: VZ has `avp-rtc` where QEMU has PL031,
  plus virtio console, entropy, socket, sound and Apple PCI devices
  (`106b:1a0x`). QEMU's guest logs `AppleVPKeyStore … ret: e00002f0`
  (kIOReturnUnsupported). The guest sets the RTC about 59 days back at each boot.
- The Data volume cannot be read on the host: its key sits in the virtual
  platform keystore (no crypto users).

## Next steps

1. Find the `reboot()` caller. The caller's thread is inside `reboot_kernel`
   during each `shutdown_stall` spindump: run `guest/stall.sh` in the guest.
   SSH windows are short; on a fresh overlay they were long enough.
2. Make guest logs survive the reset: stream `log stream` through
   `script -q /dev/null` (a pty, so line buffering) to `nc 10.0.2.2 <port>`
   with a listener on the host. `guest/diag.sh` writes to disk and loses the tail.
3. Check AppleVPKeyStore: what selectors 17 and 35 need from the platform.
4. Fix apple-gfx-mmio on a macOS 27 host (`setMapMemory:` is gone; Mohamed
   Mediouni's vmapple series on qemu-devel fixes it).
