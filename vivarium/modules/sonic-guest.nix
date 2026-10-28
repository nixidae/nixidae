# A guest that boots SONiC VS (the virtual-switch build) under QEMU.
#
# SONiC runs its own kernel and the switch stack (SWSS, syncd, FRR), so
# the vivarium agent cannot run there. The host drives it through the
# serial console with ConsoleControl and the SONiC dialect, a Linux login
# shell (docs/design/console-guests.md).
#
# As with `vivarium.image`, the node's NixOS system is never built.
{
  config,
  lib,
  ...
}:
let
  cfg = config.vivarium.sonic;
  inherit (lib) mkOption types;
in
{
  options.vivarium.sonic = mkOption {
    default = null;
    description = ''
      Boot SONiC VS instead of NixOS. QEMU backend only. Take one from
      `vivarium.images` (it carries the disk and its format).

      `execute` and `succeed` run a shell on the guest, through the serial
      console. The agent's calls (`unit_state`, `journal`, `add_closure`,
      ...) refuse.
    '';
    type = types.nullOr (
      types.submodule {
        options = {
          disk = mkOption {
            type = types.path;
            description = "The SONiC VS disk image.";
          };
          format = mkOption {
            type = types.enum [
              "raw"
              "qcow2"
            ];
            default = "qcow2";
            description = "The disk image's format, for QEMU's backing file.";
          };
        };
      }
    );
  };

  config = lib.mkIf (cfg != null) {
    assertions = [
      {
        assertion = config.vivarium.backend == "qemu";
        message = "vivarium.sonic boots under QEMU only; this guest is ${config.vivarium.backend}.";
      }
      {
        assertion =
          config.vivarium.image == null && config.vivarium.vyos == null && config.vivarium.routeros == null;
        message = "vivarium.sonic, vivarium.image, vivarium.vyos and vivarium.routeros each name the guest's root.";
      }
    ];
    # vrnetlab runs SONiC VS in 4 GiB and two cores.
    vivarium.memory = lib.mkDefault "4096M";
    vivarium.cpus = lib.mkDefault 2;
    system.build.vivariumImageGuest = {
      control = "sonic";
      inherit (cfg) format;
    };
    system.build.vivariumImageDisk = cfg.disk;
  };
}
