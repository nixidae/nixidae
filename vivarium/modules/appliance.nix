# A guest that boots a vendor's disk image under QEMU, with no NixOS and no
# agent: `vivarium.appliance`. Nothing drives it (`vivarium.control.kind =
# "none"`); its console's `ready` line says it is up.
#
# The node is still a NixOS evaluation, for the options every guest has
# (memory, cpus, interfaces, forwards). Its system is never built.
{
  config,
  lib,
  ...
}:
let
  cfg = config.vivarium.appliance;
  inherit (lib) mkOption types;
in
{
  options.vivarium.uplink.enable = mkOption {
    type = types.bool;
    default = true;
    description = ''
      Whether the guest gets passt's uplink, `vec0`: NAT to the host's
      network and the `inbound` forwards. Off, an appliance's first port is
      its first segment, as on a router whose only cables are the ones the
      test lays. Only `vivarium.appliance` honours it; `uplink.segment` is
      the other way to replace passt.
    '';
  };

  options.vivarium.appliance = mkOption {
    default = null;
    description = ''
      A disk image QEMU boots (`vivarium.backend = "qemu"`). The image's own kernel
      runs it, so nothing of vivarium's is inside: no `succeed`, no
      journal, no `/artifacts`. A test reaches it over the network,
      through `vivarium.inbound` on the uplink or from a NixOS guest on a
      segment.

      NICs follow `interfaces` order after the uplink, so a guest that
      numbers its ports by bus order sees the uplink as its first port.
    '';
    example = lib.literalExpression ''
      {
        disk = vivarium.images.routeros-chr-7_24_5;
        ready = "MikroTik [0-9.]+ ";
      }
    '';
    type = types.nullOr (
      types.submodule {
        options = {
          disk = mkOption {
            type = types.path;
            description = "The image. Read-only in the store; each run writes to a layer over it.";
          };
          format = mkOption {
            type = types.enum [
              "raw"
              "qcow2"
            ];
            default = "raw";
          };
          kernel = mkOption {
            type = types.nullOr types.path;
            default = null;
            description = ''
              A kernel to boot directly, with `initrd` and `cmdline`, for an
              image whose own boot loader cannot be told what to do. Null
              boots the disk.
            '';
          };
          initrd = mkOption {
            type = types.nullOr types.path;
            default = null;
          };
          cmdline = mkOption {
            type = types.str;
            default = "";
          };
          ready = mkOption {
            type = types.str;
            description = ''
              A Python regular expression that, found in a console line,
              says the guest is up. Escape codes are stripped first. A line
              ends at a newline, so a prompt waiting at the end of the
              console never matches: name the line before it.
            '';
          };
        };
      }
    );
  };

  config.vivarium = lib.mkIf (cfg != null) {
    kind = "appliance";
    rootImage = cfg.disk;
    closure = null;
    nixDatabase.enable = false;
    control = {
      kind = "none";
      config = { inherit (cfg) ready; };
    };
    boot = {
      root = { inherit (cfg) format; };
      kernel = lib.mkIf (cfg.kernel != null) {
        image = "${cfg.kernel}";
        initrd = "${cfg.initrd}";
        inherit (cfg) cmdline;
      };
    };
    launch.config.uplink = lib.mkIf (config.vivarium.backend == "qemu") config.vivarium.uplink.enable;
  };

  config.assertions = [
    {
      assertion = cfg != null -> config.vivarium.backend == "qemu";
      message = "vivarium.appliance boots under QEMU: set vivarium.backend = \"qemu\". This guest is ${config.vivarium.backend}.";
    }
    {
      assertion = config.vivarium.uplink.enable || cfg != null;
      message = "vivarium.uplink.enable = false: only vivarium.appliance can leave the uplink out.";
    }
    {
      assertion = config.vivarium.uplink.enable || config.vivarium.inbound == [ ];
      message = "vivarium.inbound needs the uplink: forwards are passt's.";
    }
  ];
}
