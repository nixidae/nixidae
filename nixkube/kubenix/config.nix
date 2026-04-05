# SPDX-License-Identifier: MIT

{
  config,
  lib,
  csiPkgs,
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
        pynixd.nixConfig.settings = sharedSettings // {
          max-jobs = 0;
        };
        builders.nixConfig.settings = sharedSettings // {
          max-jobs = "auto";
        };
      };
    kubernetes.resources.${cfg.namespace} = {
      ConfigMap.nix-node = {
        metadata.labels = cfg.labels;
        data = {
          "nix.conf" = builtins.readFile (cfg.node.nixConfig.nixConf);
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
      ConfigMap.pynixd = {
        metadata.labels = cfg.labels;
        data = {
          "nix.conf" = builtins.readFile (cfg.pynixd.nixConfig.nixConf);
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
