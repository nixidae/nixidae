# SPDX-License-Identifier: MIT

{
  config,
  lib,
  mkNCSI,
  x86Pkgs,
  armPkgs,
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
            "read-only-local-store"
          ];
          builders-use-substitutes = true;
          narinfo-cache-negative-ttl = 0;
          narinfo-cache-positive-ttl = 0;
          warn-dirty = false;
          store = "daemon";
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
        };
      };
    kubernetes.resources.${cfg.namespace} = {
      ConfigMap.push = lib.mkIf cfg.push (mkNCSI {
        data = {
          builder-aarch64-linux = armPkgs.nix-csi-builder-env;
          builder-x86_64-linux = x86Pkgs.nix-csi-builder-env;
          cache-aarch64-linux = armPkgs.nix-csi-cache-env;
          cache-x86_64-linux = x86Pkgs.nix-csi-cache-env;
          node-aarch64-linux = armPkgs.nix-csi-node-env;
          node-x86_64-linux = x86Pkgs.nix-csi-node-env;
          proxy-aarch64-linux = armPkgs.nix-csi-proxy-env;
          proxy-x86_64-linux = x86Pkgs.nix-csi-proxy-env;
        };
      });
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
