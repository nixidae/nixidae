# A guest with no NixOS in it: its own kernel booted straight into
# `dinix-boot`, an ephemeral root disk, the host's store, and dinit as PID 1
# running a dinix service tree (docs/design/dinix-guests.md).
#
# As with the other guest kinds, the node's NixOS system is never built:
# its evaluation is there for vivarium's own options.
{ sources }:
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.vivarium.dinix;
  inherit (lib) mkOption types;
  krun = config.vivarium.backend == "krun";

  agent = config.system.build.vivariumAgentPackage;
  krunExit = pkgs.callPackage ../pkgs/krun-exit/package.nix { };
  kernel = pkgs.callPackage ../pkgs/direct-kernel { inherit (config.boot.kernelPackages) kernel; };
  vmlinux = pkgs.callPackage ../pkgs/vmlinux/package.nix { inherit kernel; };

  # `dinitctl` for a test's commands, as a NixOS guest has `systemctl`. Not
  # dinix's systemWrapper, which names the services: the agent's service
  # names this path, so that would be a cycle.
  dinitctl =
    pkgs.runCommand "vivarium-dinitctl" { nativeBuildInputs = [ pkgs.makeBinaryWrapper ]; }
      ''
        makeBinaryWrapper ${lib.getExe' dinix.config.package "dinitctl"} $out/bin/dinitctl \
          --set-default DINIT_SOCKET_PATH ${dinix.config.socketPath}
      '';

  # What `succeed` finds on PATH.
  guestPath = pkgs.buildEnv {
    name = "vivarium-dinix-path";
    paths = cfg.packages ++ [ dinitctl ];
  };

  base = {
    name = "vivarium-${config.networking.hostName}";
    services = {
      vivarium-agent = {
        type = "process";
        command = lib.concatStringsSep " " [
          "${pkgs.coreutils}/bin/env"
          "VIVARIUM_AGENT_DEVICE=virtio-port:vivarium-agent"
          "VIVARIUM_GUEST_PATH=${guestPath}/bin"
          "VIVARIUM_INIT=dinit"
          "VIVARIUM_DINIT_SOCKET=${dinix.config.socketPath}"
          (lib.getExe agent)
        ];
        dinix.critical = false;
      };
      # A reset with no orderly stop before it is a crash. The stop runs in
      # every shutdown, before dinit unmounts the store.
      vivarium-krun-exit = lib.mkIf krun {
        type = "scripted";
        command = "${lib.getExe krunExit} /host/nix ${toString krunExit.codes.crash}";
        stop-command = "${lib.getExe krunExit} /host/nix ${toString krunExit.codes.poweroff}";
        dinix.critical = false;
      };
    };
  };

  dinix = import sources.dinix {
    inherit pkgs;
    modules = [ base ] ++ cfg.modules;
  };

  # dinix-boot's steps, tab separated. Every mount point is in the image,
  # so the root needs no write before dinit starts. The kernel mounts /dev
  # itself: nixpkgs' kernel config sets `DEVTMPFS_MOUNT`.
  spec = pkgs.writeText "dinix-boot.spec" (
    lib.concatMapStrings (step: lib.concatStringsSep "\t" step + "\n") [
      [
        "mount"
        "proc"
        "/proc"
        "proc"
        "nosuid,nodev,noexec"
      ]
      [
        "mount"
        "sysfs"
        "/sys"
        "sysfs"
        "nosuid,nodev,noexec"
      ]
      [
        "mount"
        "tmpfs"
        "/run"
        "tmpfs"
        "nosuid,nodev,mode=0755"
      ]
      # One overlay over the host's store, as modules/qemu.nix mounts it.
      [
        "mount"
        "nix"
        "/host/nix"
        "virtiofs"
        "ro,dax=always"
      ]
      [
        "mount"
        "overlay"
        "/nix"
        "overlay"
        "lowerdir=/host/nix,upperdir=/.nix-upper,workdir=/.nix-work"
      ]
      # The runner serves it only when it has a directory to serve.
      [
        "mount?"
        "artifacts"
        "/artifacts"
        "virtiofs"
        "-"
      ]
      [
        "exec"
        (lib.getExe dinix.config.systemWrapper)
      ]
    ]
  );

  rootImage =
    pkgs.runCommand "vivarium-dinix-root-${config.networking.hostName}"
      {
        # A sparse file, whole as a NAR: see `vivarium-root-image` in image.nix.
        preferLocalBuild = true;
        allowSubstitutes = false;
        nativeBuildInputs = [
          pkgs.e2fsprogs
          pkgs.fakeroot
        ];
      }
      ''
        mkdir -p root/{dev,proc,sys,run,tmp,nix,host/nix,.nix-upper,.nix-work,artifacts,bin,etc,root,var}
        chmod 1777 root/tmp
        install -m 0755 ${dinix.config.bootPackage}/bin/dinix-boot root/dinix-boot
        install -m 0644 ${spec} root/dinix-boot.spec
        # The agent runs a command through /bin/sh.
        ln -s ${pkgs.bash}/bin/sh root/bin/sh
        truncate -s ${toString config.vivarium.diskSize}M disk.img
        fakeroot -- sh -c 'chown -R 0:0 root && mkfs.ext4 -q -L dinix -d root disk.img'
        mv disk.img $out
      '';

  cmdline = lib.concatStringsSep " " (
    config.system.build.vivariumKernelParams
    ++ [
      "root=/dev/vda"
      "rootfstype=ext4"
      "rw"
      "init=/dinix-boot"
      "DINIX_SPEC=/dinix-boot.spec"
    ]
  );
in
{
  options.vivarium.dinix = mkOption {
    default = null;
    description = ''
      Run dinit as PID 1 with a dinix service tree instead of NixOS. krun
      backend only, so far.

      `execute` and `succeed` run `/bin/sh -c` with `packages` and
      `dinitctl` on PATH. `system_state` and the power-off at teardown ask
      dinit; the agent's systemd calls (`unit_state`, `journal`, ...)
      refuse.
    '';
    type = types.nullOr (
      types.submodule {
        options = {
          modules = mkOption {
            type = types.listOf types.deferredModule;
            default = [ ];
            description = "dinix modules: the guest's services.";
          };
          packages = mkOption {
            type = types.listOf types.package;
            default = [ pkgs.coreutils ];
            defaultText = lib.literalExpression "[ pkgs.coreutils ]";
            description = "What a command the host runs finds on PATH.";
          };
        };
      }
    );
  };

  config = lib.mkIf (cfg != null) {
    assertions = [
      {
        assertion = krun;
        message = "vivarium.dinix boots under krun only so far; this guest is ${config.vivarium.backend}.";
      }
    ];
    vivarium.kind = "dinix";
    vivarium.rootImage = rootImage;
    vivarium.closure = pkgs.closureInfo {
      rootPaths = [
        dinix.config.systemWrapper
        agent
        guestPath
        pkgs.bash
      ]
      ++ lib.optional krun krunExit;
    };
    vivarium.nixDatabase.enable = false;
    # The store is virtiofs, which serves a file as the guest user that
    # made it: see modules/qemu.nix.
    vivarium.requiredSystemFeatures = [ "uid-range" ];
    # For CI's kernel job (`packages.directKernel`).
    system.build.dinixKernel = kernel;
    vivarium.launch.config.boot = lib.mkIf krun {
      inherit vmlinux cmdline;
      acpi = pkgs.stdenv.hostPlatform.isx86_64;
    };
  };
}
