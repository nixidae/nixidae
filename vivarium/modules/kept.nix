# `vivarium.persist`: paths a named environment keeps across its runs.
#
# One store per guest: a sparse raw disk under UML, QEMU and krun, which
# the guest formats as ext4 when it is blank, and a host directory under
# the container backend, which has no block device to give. Each path is
# a bind mount of a directory on it, so a changed list needs no new disk.
# The root stays the run's own: a changed configuration still boots, on
# the kept data. nixidae/nixidae#1588.
{
  config,
  lib,
  pkgs,
  utils,
  ...
}:
let
  cfg = config.vivarium;

  /**
    Where the guest mounts its kept store. Outside every path a guest may
    keep, so no bind mount hides it.
  */
  root = "/.vivarium-kept";

  # The serial a QEMU or krun guest finds the disk by. The runner names
  # the same one (`KEPT_SERIAL` in vivarium_runner/backends/host.py).
  serial = "vivarium-kept";

  device =
    {
      uml = "/dev/ubdb";
      qemu = "/dev/disk/by-id/virtio-${serial}";
      krun = "/dev/disk/by-id/virtio-${serial}";
    }
    .${cfg.backend} or null;

  # "/var/lib/etcd" -> "var-lib-etcd": the directory each path keeps on
  # the store. systemd's escaping, so two paths never share one.
  source = path: "${root}/${utils.escapeSystemdPath path}";
in
{
  options.vivarium = {
    persist = lib.mkOption {
      type = lib.types.listOf (lib.types.strMatching "/[^ ]+");
      default = [ ];
      example = [ "/var/lib/etcd" ];
      description = ''
        Paths this guest keeps across the runs of a named environment
        (`vivarium up NAME`), each a bind mount of a directory on one kept
        store per guest: `<state>/envs/NAME/disks/<guest>` on the host.
        `vivarium rm NAME` deletes it. Every other run, a sandboxed check
        included, gets a blank store that lasts the run, so a check proves
        the first run. The root stays the run's own.

        A phase decides whether its work is done with `vms.state`,
        `vms.previous` and `vms.skip`. NixOS guests only.
      '';
    };

    persistSize = lib.mkOption {
      type = lib.types.ints.positive;
      default = 16384;
      description = ''
        The kept disk's size in MiB, under UML, QEMU and krun. Sparse: the
        host pays for the blocks the guest writes. A disk an earlier run
        made keeps its size.
      '';
    };
  };

  config = lib.mkIf (cfg.persist != [ ]) {
    assertions = [
      {
        assertion = cfg.kind == "nixos";
        message = "vivarium.persist needs a NixOS guest: a ${cfg.kind} guest mounts nothing vivarium names.";
      }
      {
        assertion = lib.all (path: path != "/" && !(lib.hasPrefix "${root}" path)) cfg.persist;
        message = "vivarium.persist: neither / nor a path under ${root}.";
      }
    ];

    # A container guest's store is a bind mount crun makes before init.
    fileSystems = lib.mkMerge [
      (lib.mkIf (device != null) {
        ${root} = {
          inherit device;
          fsType = "ext4";
          autoFormat = true;
        };
      })
      (lib.genAttrs cfg.persist (path: {
        device = source path;
        fsType = "none";
        options = [
          "bind"
          "x-systemd.requires=vivarium-kept.service"
          "x-systemd.after=vivarium-kept.service"
        ];
      }))
    ];

    # A bind mount's source must exist, and a blank store has none.
    systemd.services.vivarium-kept = {
      description = "The kept directories of vivarium.persist";
      unitConfig = {
        DefaultDependencies = false;
        RequiresMountsFor = root;
      };
      before = [ "local-fs.target" ];
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        ExecStart = "${lib.getExe' pkgs.coreutils "mkdir"} -p ${lib.escapeShellArgs (map source cfg.persist)}";
      };
    };
  };
}
