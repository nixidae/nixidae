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

  # vivarium names an interface by its attribute, on every backend. CHR
  # names NICs ether1..N in PCI order instead, so each is renamed by the
  # MAC vivarium gave it, and gets the addresses declared for it.
  interfaces = lib.concatMapStrings (
    nic:
    ''
      /interface ethernet set [find mac-address=${nic.mac}] name=${nic.name}
    ''
    + lib.concatMapStrings (
      address:
      if lib.hasInfix ":" address then
        "/ipv6 address add address=${address} interface=${nic.name} advertise=no\n"
      else
        "/ip address add address=${address} interface=${nic.name}\n"
    ) nic.addresses
  ) config.vivarium.nics;

  # A bare ext4 filesystem, no partition table: RouterOS mounts it at once.
  # Inside an MBR table, CHR 7.24.5 does not boot at all.
  configDisk =
    pkgs.runCommand "vivarium-routeros-config-${config.networking.hostName}"
      {
        nativeBuildInputs = [ pkgs.e2fsprogs ];
        configText = interfaces + cfg.config;
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

      The uplink is `ether1`, with RouterOS's default DHCP client. Each of
      `vivarium.interfaces` has its attribute name inside RouterOS, as on
      every backend, and its `addresses`, before `config` runs.
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
              /routing ospf instance add name=default
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
