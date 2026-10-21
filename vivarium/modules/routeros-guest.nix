# A guest that boots MikroTik RouterOS (CHR) under QEMU.
#
# RouterOS runs its own kernel, so the vivarium agent cannot run there.
# The host drives it through its serial console with ConsoleControl and
# the RouterOS dialect (docs/design/console-guests.md).
#
# As with `vivarium.image`, the node's NixOS system is never built.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.vivarium.routeros;
  inherit (lib) mkOption types;

  # A bare ext4 filesystem, no partition table: RouterOS mounts it at once.
  # Inside an MBR table, CHR 7.24.5 does not boot at all.
  configDisk =
    pkgs.runCommand "vivarium-routeros-config-${config.networking.hostName}"
      {
        nativeBuildInputs = [ pkgs.e2fsprogs ];
        configText = cfg.config;
        passAsFile = [ "configText" ];
      }
      ''
        mkdir root
        cp $configTextPath root/vivarium-config.rsc
        truncate -s 8M $out
        mkfs.ext4 -q -L VIVCFG -d root $out
      '';
in
{
  options.vivarium.routeros = mkOption {
    default = null;
    description = ''
      Boot MikroTik RouterOS (CHR) instead of NixOS. QEMU backend only.
      Take one from `vivarium.images`, and give it a `config`.

      `execute` and `succeed` run RouterOS commands, not a shell. The
      agent's calls (`unit_state`, `journal`, `add_closure`, ...) refuse.

      NICs are `ether1` (the uplink, with RouterOS's default DHCP client),
      then `ether2`, ... in `vivarium.interfaces` order.
    '';
    type = types.nullOr (
      types.submodule {
        options = {
          disk = mkOption {
            type = types.path;
            description = "The CHR raw disk image.";
          };
          config = mkOption {
            type = types.lines;
            default = "";
            example = ''
              /ip address add address=10.1.0.1/24 interface=ether2
            '';
            description = ''
              RouterOS script, applied with `/import` once the console is
              up. A failing line fails the boot, with RouterOS's message.
            '';
          };
        };
      }
    );
  };

  config = lib.mkIf (cfg != null) {
    assertions = [
      {
        assertion = config.vivarium.backend == "qemu";
        message = "vivarium.routeros boots under QEMU only; this guest is ${config.vivarium.backend}.";
      }
      {
        assertion = config.vivarium.image == null && config.vivarium.vyos == null;
        message = "vivarium.routeros, vivarium.image and vivarium.vyos each name the guest's root.";
      }
    ];
    # The probe ran CHR in 256 MiB, as vrnetlab does.
    vivarium.memory = lib.mkDefault "256M";
    system.build.vivariumImageGuest = {
      control = "routeros";
      configDisk = "${configDisk}";
    };
    system.build.vivariumImageDisk = cfg.disk;
  };
}
