# A guest with no NixOS in it: dinit as PID 1 running a dinix service tree
# over the host's store (docs/design/dinix-guests.md). Under krun, its own
# kernel boots straight into `dinix-boot` on an ephemeral root disk. In a
# container, crun execs dinix's containerWrapper, and the OCI config makes
# the mounts.
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
  container = config.vivarium.backend == "container";
  # A container's dinit runs with `--container`: a power-off ends it, and it
  # reaps every orphan as PID 1 does.
  init = if container then dinix.config.containerWrapper else dinix.config.systemWrapper;
  agentDevice = if container then config.vivarium.agentDevice else "virtio-port:vivarium-agent";

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

  # The uplink: the NIC with vec0's MAC, renamed as NixOS's networkd does,
  # leased by DHCP from the router. virtio_net and af_packet are modules in
  # the VM's kernel, and udhcpc needs a packet socket. A container has the
  # host's kernel and its vec0 from the launcher.
  uplink = pkgs.writeScript "vivarium-dinix-uplink" ''
    #!${pkgs.busybox}/bin/sh
    set -eu
    PATH=${pkgs.busybox}/bin
    ${lib.optionalString (
      !container
    ) "${pkgs.kmod}/bin/modprobe -d ${kernel.modules} -a virtio_net af_packet"}
    for dev in /sys/class/net/*; do
      if [ "$(cat "$dev/address")" = ${config.vivarium.uplinkMac} ]; then
        name=''${dev##*/}
      fi
    done
    if [ -z "''${name:-}" ]; then
      echo "no NIC has the uplink's MAC, ${config.vivarium.uplinkMac}" >&2
      exit 1
    fi
    ip link set lo up
    if [ "$name" != vec0 ]; then
      ip link set "$name" name vec0
    fi
    ip link set vec0 mtu ${toString config.vivarium.mtu} up
    exec udhcpc -f -i vec0 -s ${pkgs.busybox}/default.script
  '';

  # A `vsock` uplink: the guest is its own router, as a NixOS guest with
  # `uplink.proxy = "vsock"` is (modules/router.nix), with no router guest.
  own = config.system.build.vivariumOwnUplink or null;
  addresses = import ../nix/uplink.nix;
  ownUplink = pkgs.writeShellScript "vivarium-dinix-own-uplink" ''
    set -eu
    ${own.setup}
    PATH=${pkgs.iproute2}/bin
    ip link set lo up
    ip link set vec0 up
    ip address add ${addresses.address 0}/${toString addresses.prefixLength} dev vec0
    ip -6 address add ${addresses.address6 0}/64 dev vec0 nodad
    ip route add default via ${addresses.gateway} dev vec0
    ip -6 route add default via ${addresses.gateway6} dev vec0
    echo "nameserver ${addresses.dns}" > /etc/resolv.conf
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
          "VIVARIUM_AGENT_DEVICE=${agentDevice}"
          "VIVARIUM_GUEST_PATH=${guestPath}/bin"
          "VIVARIUM_INIT=dinit"
          "VIVARIUM_DINIT_SOCKET=${dinix.config.socketPath}"
          (lib.getExe agent)
        ];
        dinix.critical = false;
      };
      vivarium-uplink =
        if own == null then
          {
            type = "process";
            command = "${uplink}";
            dinix.critical = false;
          }
        else
          {
            type = "scripted";
            command = "${ownUplink}";
            dinix.critical = false;
          };
      # dinit has no network namespace setting: nsenter puts the relay in.
      vivarium-relay = lib.mkIf (own != null) {
        type = "process";
        command = lib.concatStringsSep " " (
          [ "${pkgs.coreutils}/bin/env" ]
          ++ lib.mapAttrsToList (name: value: "${name}=${value}") own.environment
          ++ [
            "${pkgs.util-linux}/bin/nsenter"
            "--net=${own.netns}"
            own.relay
          ]
        );
        depends-on = [ "vivarium-uplink" ];
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

  # A container's root is a directory, which the runner copies per run. The
  # mount points are the OCI config's; the overlay's two directories are
  # modules/container.nix's.
  rootDirectory = pkgs.runCommand "vivarium-dinix-root-${config.networking.hostName}" { } ''
    mkdir -p $out/{bin,etc,root,var,artifacts,nix/store,.nix-upper,.nix-work}
    ln -s ${pkgs.bash}/bin/sh $out/bin/sh
    # `ip netns add` writes /var/run/netns, as NixOS links it.
    ln -s ../run $out/var/run
  '';

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
      Run dinit as PID 1 with a dinix service tree instead of NixOS. krun and
      container backends only, so far.

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
        assertion = krun || container;
        message = "vivarium.dinix boots under krun and in a container only so far; this guest is ${config.vivarium.backend}.";
      }
    ];
    vivarium.kind = "dinix";
    # A container guest runs the relay itself. A krun guest stays behind
    # the router, where vec0 leases its address by DHCP, until its kernel
    # is checked for veth and nftables.
    vivarium.uplink.proxy = lib.mkDefault (if container then "vsock" else "router");
    vivarium.uplink.wantsRouter = lib.mkDefault (
      config.vivarium.uplink.proxy == "router" && config.vivarium.uplink.segment == null
    );
    vivarium.rootImage = if container then rootDirectory else rootImage;
    vivarium.closure = pkgs.closureInfo {
      rootPaths = [
        init
        agent
        guestPath
        pkgs.bash
      ]
      ++ lib.optional krun krunExit
      # The launcher execs the init through it, inside the container.
      ++ lib.optional container config.vivarium.launch.config.setpriv;
    };
    vivarium.nixDatabase.enable = false;
    # The store is virtiofs, which serves a file as the guest user that
    # made it: see modules/qemu.nix.
    vivarium.requiredSystemFeatures = [ "uid-range" ];
    # For CI's kernel job (`packages.directKernel`).
    system.build.dinixKernel = kernel;
    # No initrd: the kernel mounts the root disk itself.
    vivarium.boot =
      if container then
        { init = lib.getExe init; }
      else
        {
          kernel = {
            image = "${kernel}/${config.system.boot.loader.kernelFile}";
            vmlinux = "${vmlinux}";
            inherit cmdline;
          };
        };
  };
}
