{
  config,
  pkgs,
  lib,
  mkNCSI,
  ...
}:
let
  cfg = config.nix-csi;

in
{
  config = lib.mkIf cfg.enable {
    nix-csi =
      let
        sharedSettings = {
          allowed-users = [ "*" ];
          trusted-users = [
            "root"
            "nix"
          ];
          experimental-features = [
            "nix-command"
            "flakes"
            "auto-allocate-uids"
            "read-only-local-store"
          ];
          auto-allocate-uids = true;
          builders-use-substitutes = true;
          narinfo-cache-negative-ttl = 0;
          narinfo-cache-positive-ttl = 0;
          warn-dirty = false;
        };
      in
      {
        node.nixConfig.settings = sharedSettings // {
          keep-outputs = true; # Remove when we have separate builders
        };
        cache.nixConfig.settings = sharedSettings // {
          max-jobs = 0;
        };
        builders.nixConfig.settings = sharedSettings // {
          max-jobs = "auto";
          keep-outputs = true;
        };
      };
    kubernetes.resources.${cfg.namespace} = {
      ConfigMap.nix-node = mkNCSI {
        data = {
          "nix.conf" = builtins.readFile (cfg.node.nixConfig.nixConf);
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
      ConfigMap.nix-cache = mkNCSI {
        data = {
          "nix.conf" = builtins.readFile (cfg.cache.nixConfig.nixConf);
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
      ConfigMap.nix-builder = mkNCSI {
        data = {
          "nix.conf" = builtins.readFile (cfg.builders.nixConfig.nixConf);
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
    };
  };
}
