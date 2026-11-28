/*
  A guest's place in the lab, for `vivarium.recipes.lab`
  (nixidae/nixidae#1669): the names the router's proxy answers for it, the
  ports those names lead to, and a certificate from the run's lab CA.

  The certificate exists only once the run does: the recipe's `lab` phase
  writes it. So each unit in `tls.units` waits for it. A path unit starts
  `vivarium-tls.target` when the key is there, and the units start with
  the target, in the group that reads the key. Until then
  `ConditionPathExists` skips them, which is not a failure, so `boot`
  passes. After a reboot or a switch the key is still
  there, and they start again.
*/
{ config, lib, ... }:
let
  cfg = config.vivarium.lab;
  inherit (lib) mkOption types;
  key = "${cfg.tls.directory}/key.pem";
in
{
  options.vivarium.lab = {
    names = mkOption {
      type = types.attrsOf (
        types.submodule {
          options.ports = {
            https = mkOption {
              type = types.nullOr types.port;
              default = null;
              example = 8443;
              description = ''
                The port that `https://<name>/` reaches, when it is not 443: an
                SRV record `_https._tcp.<name>`, which the router's proxy
                follows. A client inside the lab does not follow it; it names
                the port.
              '';
            };
            http = mkOption {
              type = types.nullOr types.port;
              default = null;
              example = 8080;
              description = "As `https`, for `http://<name>/` and port 80.";
            };
          };
        }
      );
      default = { };
      example = lib.literalExpression "{ api.ports.https = 8443; }";
      description = ''
        The guest's names under the lab's domain (`vivarium.recipes.lab.domain`),
        beside its host name, which it always has: `api` is `api.lab.test`.
        The router's proxy answers them, and every guest has them in
        `/etc/hosts`.
      '';
    };

    tls = {
      enable = lib.mkEnableOption ''
        a certificate for the guest's names, from the run's lab CA. The
        `lab` phase writes `cert.pem` (the certificate and its intermediate)
        and `key.pem` to `directory`
      '';
      units = mkOption {
        type = types.listOf types.str;
        default = [ ];
        example = [ "nginx" ];
        description = ''
          Services that read the certificate: each starts once it is there,
          not before.
        '';
      };
      directory = mkOption {
        type = types.str;
        readOnly = true;
        default = "/var/lib/vivarium/tls";
        description = "Where the certificate and its key are.";
      };
      group = mkOption {
        type = types.str;
        readOnly = true;
        default = "vivarium-tls";
        description = "The group that reads the key. Each unit in `units` has it.";
      };
    };
  };

  config = lib.mkMerge [
    { vivarium.lab.names.${config.networking.hostName} = { }; }
    (lib.mkIf cfg.tls.enable {
      systemd.targets.vivarium-tls.description = "The guest's lab certificate is in place";
      systemd.paths.vivarium-tls = {
        wantedBy = [ "multi-user.target" ];
        pathConfig = {
          PathExists = key;
          Unit = "vivarium-tls.target";
        };
      };
      # The key is root's and the group's: a unit reads it however it drops
      # privileges. nginx tests its configuration as `nginx`.
      users.groups.${cfg.tls.group} = { };
      systemd.services = lib.genAttrs cfg.tls.units (_: {
        wantedBy = [ "vivarium-tls.target" ];
        after = [ "vivarium-tls.target" ];
        unitConfig.ConditionPathExists = key;
        serviceConfig.SupplementaryGroups = [ cfg.tls.group ];
      });
    })
  ];
}
