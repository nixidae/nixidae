# SPDX-License-Identifier: MIT

{
  config,
  lib,
  x86Pkgs,
  armPkgs,
  ...
}:
let
  cfg = config.nixkube;

in
{
  config = lib.mkIf cfg.enable {
    nixkube =
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
      ConfigMap.push = lib.mkIf cfg.push {
        metadata.labels = cfg.labels;
        data = {
          builder-aarch64-linux = armPkgs.nixkube-builder-env;
          builder-x86_64-linux = x86Pkgs.nixkube-builder-env;
          cache-aarch64-linux = armPkgs.nixkube-cache-env;
          cache-x86_64-linux = x86Pkgs.nixkube-cache-env;
          node-aarch64-linux = armPkgs.nixkube-node-env;
          node-x86_64-linux = x86Pkgs.nixkube-node-env;
          proxy-aarch64-linux = armPkgs.nixkube-proxy-env;
          proxy-x86_64-linux = x86Pkgs.nixkube-proxy-env;
        };
      };
      ConfigMap.nix-node = {
        metadata.labels = cfg.labels;
        data = {
          "nix.conf" = builtins.readFile (cfg.node.nixConfig.nixConf);
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
      ConfigMap.nix-cache = {
        metadata.labels = cfg.labels;
        data = {
          "nix.conf" = builtins.readFile (cfg.cache.nixConfig.nixConf);
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
      ConfigMap.nix-builder = {
        metadata.labels = cfg.labels;
        data = {
          "nix.conf" = builtins.readFile (cfg.builders.nixConfig.nixConf);
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
    };
  };
}
