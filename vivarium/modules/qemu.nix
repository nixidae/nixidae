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
  options,
  lib,
  pkgs,
  hostPkgs,
  ...
}:
let
  cfg = config.vivarium;
  tools = import ./tools.nix {
    pkgs = hostPkgs;
    inherit lib;
  };
  krun = cfg.backend == "krun";
  # libkrun 2.0 describes an x86 guest's devices in ACPI on request, and
  # ends on the guest's S5. Its ACPI tables are x86 only.
  krunAcpi = krun && pkgs.stdenv.hostPlatform.isx86_64;
  # OVMF measures the boot into a TPM only when built with `tpmSupport`,
  # and OVMFFull is the build with it that the binary cache holds.
  ovmf = if cfg.secureBoot || cfg.tpm.enable then pkgs.OVMFFull else pkgs.OVMF;

  hex = n: (lib.optionalString (n < 16) "0") + lib.toHexString n;

  vmlinux = pkgs.callPackage ../pkgs/vmlinux/package.nix {
    inherit (config.boot.kernelPackages) kernel;
  };

  setKrunExit = pkgs.callPackage ../pkgs/krun-exit/package.nix { };
  krunExit = setKrunExit.codes;
  # Runs while /host/nix is still mounted: the unit starts after the mount,
  # so it stops before it.
  krunExitOnStop = pkgs.writeShellScript "vivarium-krun-exit-stop" ''
    code=${toString krunExit.poweroff}
    while read -r _ unit _; do
      if [ "$unit" = reboot.target ]; then
        code=${toString krunExit.reboot}
      fi
    done < <(${config.systemd.package}/bin/systemctl list-jobs --no-legend --plain)
    exec ${lib.getExe setKrunExit} /host/nix "$code"
  '';
in
lib.mkMerge [
  {
    vivarium.backendNames = [
      "qemu"
      "krun"
    ];
  }
  (lib.mkIf
    (lib.elem cfg.backend [
      "qemu"
      "krun"
    ])
    {
      # ttyS0 is the console here, not the control channel.  A stock kernel
      # prints to it from the first line, and a virtio port does not exist
      # until its driver loads -- so a panic before that would be invisible.
      # The agent takes a named virtio-serial port, and udev links it by the
      # name the launcher gave it. Not a console port (hvc0): QEMU drops what
      # its socket cannot take at once (nixidae/nixidae#1178), and under OVMF
      # it stops delivering what the host sends once Linux has it.
      vivarium.agentDevice = "/dev/virtio-ports/vivarium-agent";

      vivarium.launch =
        if krun then
          {
            backend = "krun";
            config = {
              inherit (tools) krun qemuImg;
              acpi = krunAcpi;
            };
          }
        else
          {
            backend = "qemu";
            config = {
              inherit (tools)
                qemu
                qemuImg
                virtiofsd
                ;
            }
            // lib.optionalAttrs cfg.display.enable { inherit (tools) tesseract magick; }
            // lib.optionalAttrs cfg.tpm.enable { inherit (tools) swtpm; }
            // lib.optionalAttrs (cfg.disks != [ ]) {
              disks = map (disk: { inherit (disk) name size; }) cfg.disks;
            };
          };

      /*
        KVM, because a QEMU or libkrun guest is only worth booting with it.
        `uid-range` too: virtiofsd serves a file as the guest user that made
        it, and in a sandbox that maps one uid it cannot become uid 1000
        (EINVAL, measured: `artifacts.qemu`). A guest that boots its own disk
        runs no virtiofsd.

        On macOS, krun runs on Hypervisor.framework, which a daemon offers as
        `apple-virt` (Nix 2.19). libkrun serves the store itself, with no
        virtiofsd, and a macOS daemon has no `uid-range` to give.
      */
      vivarium.requiredSystemFeatures =
        if hostPkgs.stdenv.hostPlatform.isDarwin then
          [ "apple-virt" ]
        else
          [ "kvm" ] ++ lib.optional (cfg.kind == "nixos") "uid-range";

      # Without ACPI, libkrun's machine has no power-off: a poweroff only
      # halts the CPU, and the process stays up at 100% of a core (measured:
      # `reboot: Power off not available: System halted instead`). With
      # `krunAcpi`, a poweroff enters S5 and ends libkrun. A halt still only
      # stops the CPU, so it resets, as a poweroff does without ACPI; a reset
      # through the i8042 ends libkrun, the way Firecracker's guests stop.
      systemd.services.systemd-poweroff.unitConfig.SuccessAction = lib.mkIf (
        krun && !krunAcpi
      ) "reboot-force";
      systemd.services.systemd-halt.unitConfig.SuccessAction = lib.mkIf krun "reboot-force";

      # A reset with no shutdown before it is a crash: a panic, or a guest
      # that reset itself. The stop runs inside every shutdown and replaces
      # that with what the shutdown is for.
      systemd.services.vivarium-krun-exit = lib.mkIf krun {
        description = "Tell libkrun how the guest stops";
        wantedBy = [ "sysinit.target" ];
        requires = [ "host-nix.mount" ];
        after = [ "host-nix.mount" ];
        before = [
          "sysinit.target"
          "shutdown.target"
        ];
        conflicts = [ "shutdown.target" ];
        unitConfig.DefaultDependencies = false;
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
          ExecStart = "${lib.getExe setKrunExit} /host/nix ${toString krunExit.crash}";
          ExecStop = krunExitOnStop;
        };
      };

      boot.kernelParams = config.system.build.vivariumKernelParams;
      # vivarium's own, apart from what NixOS adds: a guest kind's kernel takes
      # these and none of NixOS's (`root=fstab`).
      system.build.vivariumKernelParams =
        lib.optionals krun [
          "reboot=k"
          "panic=-1"
        ]
        ++ [
          # A PL011 on aarch64: libkrun's serial port, and QEMU virt's.
          (if pkgs.stdenv.hostPlatform.isAarch64 then "console=ttyAMA0,115200" else "console=ttyS0,115200")
          # What the balloon reports as free, in pages: 2^5 is 128 KiB.
          #
          # The default is `pageblock_order`, 2 MiB here, and a guest that has
          # just dropped its page cache holds most of its free memory in
          # smaller pieces than that -- measured, 254 MB of 552 MB came back at
          # the default. `mm/page_reporting.c` exposes this as a parameter of
          # the built-in `page_reporting` "module", and it overrides whatever
          # virtio-balloon asks for.
          "page_reporting.page_reporting_order=5"
          # The kernel gives a parameter with no dot to init as environment.
          # systemd holds back every mount job while its mount monitor is
          # rate limited, 5 changes per second by default (`src/core/mount.c`).
          # The initrd changes the mount table 22 times, so the default
          # holds /sysroot back 1.00 s, and /sysroot/nix 1.00 s after it.
          # Measured, "Welcome" after the initrd banner: 3.8 s by default,
          # 2.35-2.77 s with this on `lan.qemu`, 2.67-2.72 s on `lan.krun`.
          "SYSTEMD_DEFAULT_MOUNT_RATE_LIMIT_BURST=50"
        ]
        # DAMON_RECLAIM's stock watermarks keep it idle while more than half
        # of memory is free, which is right on a host and wrong here: a free
        # guest page costs the host nothing once reported, and a cached one
        # does. So it runs at any free level, and stops below 5% free, where
        # the kernel's own reclaim takes over.
        ++ lib.optionals cfg.reclaim.enable [
          "damon_reclaim.min_age=${toString (cfg.reclaim.minAge * 1000000)}"
          "damon_reclaim.wmarks_high=1000"
          "damon_reclaim.wmarks_mid=999"
          "damon_reclaim.wmarks_low=50"
          "damon_reclaim.enabled=Y"
        ];

      # systemd in the initrd, so the overlay below gets its upperdir and
      # workdir created and its ordering worked out for it. A nixpkgs without
      # the scripted initrd keeps the option as a removed stub, of type
      # unspecified, that refuses any definition.
      boot.initrd.systemd.enable = lib.mkIf (options.boot.initrd.systemd.enable.type.name == "bool") true;
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
      system.build.vivariumRootImage =
        pkgs.runCommand "qemu-root-image"
          {
            # See `vivarium-root-image` in image.nix.
            preferLocalBuild = true;
            allowSubstitutes = false;
            nativeBuildInputs = [
              pkgs.e2fsprogs
              pkgs.fakeroot
            ];
          }
          ''
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
            # As large as what it holds, not `vivarium.diskSize`: the store
            # keeps no holes, so that size is that many bytes in the store,
            # written and hashed again for every change to the guest (20 GiB
            # of zeros for a 20 GiB disk, 133 MB of it data). The runner's
            # layer over it is diskSize (`scratch_disk`), and the root grows
            # to it at boot (`autoResize` below). Twice the contents and 64
            # MiB covers ext4's journal and metadata.
            size=$(( $(du -sm root | cut -f1) * 2 + 64 ))
            truncate -s "''${size}M" disk.img
            fakeroot -- sh -c 'chown -R 0:0 root && mkfs.ext4 -q -L nixos -d root disk.img'
            mv disk.img $out
          '';

      fileSystems."/" = {
        # The only disk, so name it directly rather than waiting for udev to
        # find a label.
        device = "/dev/vda";
        fsType = "ext4";
        # mkfs made it at build time and every run gets a new overlay, so a
        # check finds nothing. The kernel replays the journal at mount.
        noCheck = true;
        # systemd-growfs: from the image's size to the disk's, online.
        autoResize = true;
      };

      /*
        The EFI system partition for `vivarium.efi`, on a disk of its own so
        the root stays one ext4 without a partition table.

        GPT with the ESP type, because `bootctl install` refuses a vfat that is
        not on one. qcow2, because the store keeps no holes and the raw image
        is 512 MiB of them; the runner layers a scratch qcow2 over it as over
        the root.
      */
      system.build.vivariumEspImage =
        pkgs.runCommand "qemu-esp-image"
          {
            nativeBuildInputs = [
              pkgs.util-linux
              pkgs.dosfstools
              pkgs.qemu-utils
            ];
          }
          ''
            truncate -s 512M esp.img
            echo 'label: gpt
            start=2048, size=1044480, type=C12A7328-F81F-11D2-BA4B-00A0C93EC93B, name=ESP' | sfdisk --quiet esp.img
            mkfs.vfat -F 32 -n ESP --offset 2048 esp.img 522240 >/dev/null
            qemu-img convert -f raw -O qcow2 esp.img $out
          '';

      fileSystems."/boot" = lib.mkIf cfg.efi {
        device = "/dev/disk/by-partlabel/ESP";
        fsType = "vfat";
        options = [ "umask=0077" ];
      };

      boot.loader.efi.canTouchEfiVariables = lib.mkIf cfg.efi true;

      /*
        The first boot is a direct kernel boot, with an empty ESP. This makes
        the booted system generation 1 and installs systemd-boot for it, as
        an installer would, so the next boot is the firmware's. The ESP lives
        as long as the run, so a reboot skips it.

        Before the agent, so a test never sees the guest up with no
        bootloader.
      */
      systemd.services.vivarium-install-bootloader = lib.mkIf cfg.efi {
        description = "Install systemd-boot for the booted system";
        wantedBy = [ "multi-user.target" ];
        before = [ "vivarium-agent.service" ];
        unitConfig = {
          RequiresMountsFor = [
            "/boot"
            "/nix/var/nix"
          ];
          ConditionPathExists = "!/boot/loader/loader.conf";
        };
        environment.NIXOS_INSTALL_BOOTLOADER = "1";
        serviceConfig = {
          Type = "oneshot";
          RemainAfterExit = true;
        };
        path = [
          config.nix.package
          pkgs.coreutils
        ];
        script = ''
          nix-env --profile /nix/var/nix/profiles/system --set "$(readlink -f /run/current-system)"
          /run/current-system/bin/switch-to-configuration boot
        '';
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
          # A template's instance gets its index at run time: any of them.
          matchConfig.MACAddress =
            if cfg.template then
              lib.concatMapStringsSep " " (index: "52:54:00:12:00:${hex index}") (lib.range 0 255)
            else
              "52:54:00:12:00:${hex cfg.index}";
          linkConfig = {
            Name = "vec0";
          }
          // lib.optionalAttrs krun { MTUBytes = toString cfg.mtu; };
        };
      }
      # QEMU hands each segment interface its MTU as `host_mtu`. libkrun has
      # no such field, so the guest sets it, vec0's above too; with no
      # `VIRTIO_NET_F_MTU` the driver allows up to 65535.
      // lib.optionalAttrs krun (
        lib.listToAttrs (
          map (nic: lib.nameValuePair "10-${nic.name}" { linkConfig.MTUBytes = toString nic.mtu; }) cfg.nics
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

      # Named here rather than worked out in Python, so a path the runner
      # reads is a path Nix built. A guest kind's disk is not this NixOS.
      vivarium.boot = lib.mkIf (cfg.kind == "nixos") {
        system = "${config.system.build.toplevel}";
        kernel = {
          image = "${config.system.build.kernel}/${config.system.boot.loader.kernelFile}";
          vmlinux = if krun then "${vmlinux}" else null;
          initrd = "${config.system.build.initialRamdisk}/${config.system.boot.loader.initrdFile}";
          cmdline = lib.concatStringsSep " " config.boot.kernelParams;
        };
        firmware =
          if cfg.efi then
            {
              code = "${ovmf.firmware}";
              # The empty store: with no keys, Secure Boot firmware is in
              # Setup Mode. `variablesMs` would enroll Microsoft's.
              variables = "${ovmf.variables}";
              esp = "${config.system.build.vivariumEspImage}";
              smm = ovmf.systemManagementModeRequired;
            }
          else
            null;
      };

      # guest.nix turns the template off, which also takes logind's autovt@.
      # A screen with nothing on it is a screen nothing can be read from.
      systemd.services."getty@".enable = lib.mkIf cfg.display.enable (lib.mkForce true);
    }
  )
]
