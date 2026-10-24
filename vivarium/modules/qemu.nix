# The guest, when the backend is QEMU or libkrun.
#
# UML compiles a kernel with everything built in and boots straight into
# /init on a disk image.  QEMU boots a stock NixOS kernel with its normal
# initrd, because every driver it needs -- virtio_pci, virtio_console,
# virtiofs, overlay -- is a module in the host's kernel package and
# nothing here is worth a second kernel build.
#
# libkrun boots the same kernel, initrd and disk.  Its devices are
# virtio-mmio instead of PCI, and it serves the store from inside its own
# process with a DAX window: the guest maps the host's page cache of the
# store instead of keeping a copy of it.  Measured, three guests reading
# their 362 MiB closure: 2459 MiB of host memory under QEMU, 980 MiB
# under libkrun (nixidae/nixidae#1077).
#
# Two things stay the same as the UML guest on purpose:
#
#   * /nix is ONE overlay mount, the host's store below and a writable
#     layer above.  See modules/image.nix for why it must not be split.
#   * the host drives the guest over a serial line, so commands work
#     before networking exists and inside a build sandbox.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.vivarium;
  krun = cfg.backend == "krun";

  hex = n: (lib.optionalString (n < 16) "0") + lib.toHexString n;

  kernel = config.boot.kernelPackages.kernel;

  # libkrun loads an ELF kernel and no bzImage. The dev output's vmlinux
  # is 433 MB of debug info; stripped, the run's closure holds only this.
  vmlinux = pkgs.runCommand "vmlinux-${kernel.version}" { nativeBuildInputs = [ pkgs.binutils ]; } ''
    objcopy --strip-all ${kernel.dev}/vmlinux $out
  '';
in
lib.mkIf (lib.elem cfg.backend [
  "qemu"
  "krun"
]) {
  # ttyS0 is the console here, not the control channel.  A stock kernel
  # prints to it from the first line, and a virtio console does not exist
  # until its driver loads -- so a panic before that would be invisible.
  # The agent takes hvc0 instead. libkrun's port is not a console, so it
  # never becomes hvc0, and udev links it by the name the launcher gave it.
  vivarium.agentDevice = if krun then "/dev/virtio-ports/vivarium-agent" else "/dev/hvc0";

  # libkrun's x86 machine has no ACPI, so a poweroff only halts the CPU,
  # and the libkrun process stays up at 100% of a core until something
  # kills it (measured: `reboot: Power off not available: System halted
  # instead`). What ends libkrun is a reset through its i8042, the way
  # Firecracker's guests stop. So a krun guest resets where it would power
  # off or halt, and on a panic, which `panic=-1` turns into a reset too.
  systemd.services.systemd-poweroff.unitConfig.SuccessAction = lib.mkIf krun "reboot-force";
  systemd.services.systemd-halt.unitConfig.SuccessAction = lib.mkIf krun "reboot-force";

  boot.kernelParams =
    lib.optionals krun [
      "reboot=k"
      "panic=-1"
    ]
    ++ [
      "console=ttyS0,115200"
      # What the balloon reports as free, in pages: 2^5 is 128 KiB.
      #
      # The default is `pageblock_order`, 2 MiB here, and a guest that has
      # just dropped its page cache holds most of its free memory in
      # smaller pieces than that -- measured, 254 MB of 552 MB came back at
      # the default. `mm/page_reporting.c` exposes this as a parameter of
      # the built-in `page_reporting` "module", and it overrides whatever
      # virtio-balloon asks for.
      "page_reporting.page_reporting_order=5"
    ];

  # systemd in the initrd, so the overlay below gets its upperdir and
  # workdir created and its ordering worked out for it.
  boot.initrd.systemd.enable = true;
  boot.initrd.availableKernelModules = [
    "virtio_pci"
    "virtio_blk"
    "virtio_console"
    "virtio_net"
    "virtiofs"
    "overlay"
  ];
  boot.initrd.kernelModules = [
    "virtio_pci"
    "virtio_blk"
    "virtio_console"
    "virtiofs"
    "overlay"
  ]
  # The devices come from `virtio_mmio.device=` on the kernel line, which
  # libkrun appends, and the module reads them when it loads.
  ++ lib.optional krun "virtio_mmio";

  # The stock fuse drops DAX mappings that are still in use, and a guest
  # process gets SIGBUS: pkgs/fuse-dax says how.
  boot.extraModulePackages = lib.mkIf krun [
    (config.boot.kernelPackages.callPackage ../pkgs/fuse-dax/package.nix { })
  ];

  # The balloon, for free page reporting: the guest tells QEMU which
  # pages it has freed and QEMU madvises them out of the memfd its RAM
  # lives in.  Named rather than left to udev's PCI autoload, so that a
  # guest reports from the moment it has a root rather than from whenever
  # the modalias rule happens to fire.
  boot.kernelModules = [
    "virtio_balloon"
  ]
  ++ lib.optionals cfg.display.enable [
    "virtio_gpu"
    "virtio_input"
  ];

  /*
    A real disk, the same one UML gets, for the same reason.

    A tmpfs root is tempting here -- one less device, and a guest throws
    its root away at poweroff anyway. It is wrong, and the way it is wrong
    is invisible until a test writes something: a tmpfs is charged to the
    RAM the guest is running in, so `vivarium.diskSize` would silently
    become `vivarium.memory` and a guest that writes a few hundred MB
    would run out of memory rather than out of disk.

    Measured: nixkube's node test copies a closure into the node's own
    store at boot, and on a tmpfs root that unit fails after 87s while
    every later step reports a missing store path instead.

    The image is read-only in the store and the runner puts a per-run
    qcow2 over it, which is what UML's `ubd0=<cow>,<image>` does.
  */
  # `fakeroot` for the ownership -- modules/image.nix says why.
  system.build.vivariumRootImage = pkgs.runCommand "qemu-root-image" {
    nativeBuildInputs = [ pkgs.e2fsprogs pkgs.fakeroot ];
  } ''
    mkdir -p root/{dev,proc,sys,tmp,run,var,root,home,artifacts}
    mkdir -p root/nix root/.nix-upper/store root/.nix-work root/host/nix root/nix-state
    # Without it nix-daemon.socket is skipped silently -- see image.nix.
    mkdir -p root/nix-state/nix/daemon-socket
    ${lib.optionalString cfg.nixDatabase.enable ''
      mkdir -p root/nix-state/nix/db
      install -m 0644 ${config.system.build.vivariumNixDatabase}/db.sqlite root/nix-state/nix/db/
      install -m 0644 ${config.system.build.vivariumNixDatabase}/schema root/nix-state/nix/db/
      # See modules/image.nix: a build is where this is worth catching.
      test -s root/nix-state/nix/db/db.sqlite
      test -s root/nix-state/nix/db/schema''}
    truncate -s ${toString cfg.diskSize}M disk.img
    fakeroot -- sh -c 'chown -R 0:0 root && mkfs.ext4 -q -L nixos -d root disk.img'
    mv disk.img $out
  '';

  fileSystems."/" = {
    # The only disk, so name it directly rather than waiting for udev to
    # find a label.
    device = "/dev/vda";
    fsType = "ext4";
  };

  fileSystems."/host/nix" = {
    device = "nix";
    fsType = "virtiofs";
    neededForBoot = true;
    # QEMU's vhost-user-fs has no DAX window, and `dax=always` without one
    # refuses to mount.
    options = [ "ro" ] ++ lib.optional krun "dax=always";
  };

  fileSystems."/nix" = {
    neededForBoot = true;
    overlay = {
      lowerdir = [ "/host/nix" ];
      upperdir = "/.nix-upper";
      workdir = "/.nix-work";
    };
  };

  /*
    The same directory a UML guest gets over hostfs, so a test writes to
    `/artifacts` and never asks which backend it got.  A second virtiofsd
    serves it under the tag named here.

    `nofail`, because the runner serves it only when it has a directory to
    serve, and a guest booted without one must still come up.
  */
  fileSystems."/artifacts" = {
    device = "artifacts";
    fsType = "virtiofs";
    options = [ "nofail" ];
  };

  /*
    Name the uplink `vec0`, as UML's `vec0=` argument names it.  QEMU names
    a virtio-net device after its PCI slot, so the runner gives it a fixed
    MAC and the guest renames by it.  guest.nix does the same for each
    segment interface, on every backend.
  */
  systemd.network.links = {
    "10-vec0" = {
      matchConfig.MACAddress = "52:54:00:12:00:${hex cfg.index}";
      linkConfig.Name = "vec0";
    };
  }
  # QEMU hands each segment interface its MTU as `host_mtu`. libkrun has
  # no such field, so the guest sets it; with no `VIRTIO_NET_F_MTU` the
  # driver allows up to 65535.
  // lib.optionalAttrs krun (
    lib.listToAttrs (
      map (nic: lib.nameValuePair "10-${nic.name}" { linkConfig.MTUBytes = toString cfg.mtu; }) cfg.nics
    )
  );

  /*
    `/dev/kvm` in the guest, for its own virtual machines.

    The runner hides `vmx` and `svm` from every other guest. Measured:
    with `-cpu host` alone, udev loaded `kvm_amd` in a guest that asked
    for nothing, so every QEMU guest could run VMs.

    A unit and not `boot.kernelModules`: the module is per vendor, and
    naming both fails `systemd-modules-load` for the one that does not
    match. The host passes the flag only with nesting on, so a missing
    flag is said here, not left to a VM that fails later with a bare "no
    /dev/kvm".
  */
  systemd.services.vivarium-kvm = lib.mkIf cfg.nestedVirtualization {
    description = "Load KVM for nested virtualization";
    wantedBy = [ "multi-user.target" ];
    before = [ "vivarium-agent.service" ];
    serviceConfig = {
      Type = "oneshot";
      RemainAfterExit = true;
    };
    path = [
      pkgs.gnugrep
      pkgs.kmod
    ];
    script = ''
      if grep -qw vmx /proc/cpuinfo; then
        modprobe kvm_intel
      elif grep -qw svm /proc/cpuinfo; then
        modprobe kvm_amd
      else
        echo "the guest CPU has neither vmx nor svm: enable nesting on the host," \
          "/sys/module/kvm_{intel,amd}/parameters/nested" >&2
        exit 1
      fi
    '';
  };

  # What the runner needs to boot this guest, named here rather than
  # worked out in Python, so a path it reads is a path Nix built.
  system.build.qemuBoot = {
    kernel = "${config.system.build.kernel}/${config.system.boot.loader.kernelFile}";
    initrd = "${config.system.build.initialRamdisk}/${config.system.boot.loader.initrdFile}";
    toplevel = "${config.system.build.toplevel}";
    cmdline = lib.concatStringsSep " " config.boot.kernelParams;
    nested = cfg.nestedVirtualization;
    display = if cfg.display.enable then { inherit (cfg.display) width height; } else null;
  }
  // lib.optionalAttrs krun { vmlinux = "${vmlinux}"; };

  # guest.nix turns the template off, which also takes logind's autovt@.
  # A screen with nothing on it is a screen nothing can be read from.
  systemd.services."getty@".enable = lib.mkIf cfg.display.enable (lib.mkForce true);
}
