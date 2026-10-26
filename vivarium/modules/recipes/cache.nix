# A guest that serves every store path the run needs, and every other
# guest substituting from it and from nothing else.
#
# A guest sees only its own registered closure. Anything else it needs --
# a workload, an image's closure, a system to switch to -- it has to
# fetch, the way a real machine does. Without this recipe the only source
# is the network, which a build sandbox does not have, so a miss costs
# five 15 s timeouts and then fails. With it, a miss fails at once and
# names the path, and a run needs no network for Nix at all.
#
# The cache guest registers `roots` and serves its store with harmonia,
# signed with ./cache-facts.nix's test key. Each guest reaches it on a
# segment of its own, `cache`, so a test's own LAN stays the test's.
# `settings.cache` hands the URL and key to phases; whatever a guest deploys
# (a pod has no /etc/hosts entry for it) takes them from `cacheFacts`.
{ config, lib, ... }:
let
  cfg = config.vivarium.recipes.cache;
  facts = import ./cache-facts.nix;
  inherit (facts) address port url;
in
{
  options.vivarium.recipes.cache = {
    enable = lib.mkEnableOption "a guest that serves the run's store paths to the others";

    name = lib.mkOption {
      type = lib.types.str;
      default = "cache";
      description = "The cache guest's name in `nodes`.";
    };

    roots = lib.mkOption {
      type = lib.types.listOf lib.types.str;
      default = [ ];
      example = lib.literalExpression ''[ "''${pkgs.hello}" ]'';
      description = ''
        Store paths whose closures the cache guest serves. Each is a
        dependency of the run, which is also what puts it in the build
        sandbox.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    settings.cache = { inherit url; inherit (facts) trustedPublicKey; };

    nodes.${cfg.name} = {
      vivarium.memory = lib.mkDefault "512M";
      vivarium.interfaces.cache = {
        segment = "cache";
        addresses = [ "${address}/24" ];
      };
      vivarium.nixDatabase.extraRoots = cfg.roots;
      services.harmonia.cache = {
        enable = true;
        signKeyPaths = [ facts.secretKeyFile ];
      };
      networking.firewall.allowedTCPPorts = [ port ];
    };

    defaults =
      {
        config,
        lib,
        pkgs,
        ...
      }:
      lib.mkIf (config.networking.hostName != cfg.name) {
        vivarium.interfaces.cache = {
          segment = "cache";
          addresses = [ "10.254.0.${toString (config.vivarium.index + 2)}/24" ];
        };
        nix.settings.substituters = lib.mkForce [ url ];
        nix.settings.trusted-public-keys = [ facts.trustedPublicKey ];

        # Before the guest counts as booted, and before kubelet, so nothing
        # asks for a path while the cache guest is still starting.
        systemd.services.vivarium-cache-online = {
          description = "Wait for the run's cache guest to answer";
          wantedBy = [ "multi-user.target" ];
          before = [
            "multi-user.target"
            "kubelet.service"
          ];
          after = [ "network-online.target" ];
          wants = [ "network-online.target" ];
          serviceConfig = {
            Type = "oneshot";
            RemainAfterExit = true;
            TimeoutStartSec = 120;
          };
          script = ''
            until ${lib.getExe pkgs.curl} --silent --fail --max-time 2 \
              ${url}/nix-cache-info >/dev/null; do
              sleep 0.2
            done
          '';
        };
      };
  };
}
