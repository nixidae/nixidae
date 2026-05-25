# SPDX-License-Identifier: MIT

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;

in
{
  config = lib.mkIf cfg.enable {
    nixkube = {
      nixConfig.settings = {
        allowed-users = [ "*" ];
        trusted-public-keys = [
          "nix-csi.cachix.org-1:i4w33gR4efO67jpz8U7g/MdvRQ6mQ3LEF9fB8tES60g="
        ];
        substituters = [
          "https://nix-csi.cachix.org"
        ];
        experimental-features = [
          "nix-command"
          "flakes"
          "read-only-local-store"
          "ca-derivations"
          "dynamic-derivations"
          "recursive-nix"
        ];
        builders-use-substitutes = true;
        narinfo-cache-negative-ttl = 0;
        narinfo-cache-positive-ttl = 0;
        warn-dirty = false;
        store = "daemon";
        trusted-users = [ "root" ];
        system-features = [
          "nixos-test"
          "benchmark"
          "big-parallel"
        ];
      };
      node.nixConfig.settings = lib.mapAttrsRecursive (
        name: value: lib.mkDefault value
      ) cfg.nixConfig.settings;
      pynixd.controller.nixConfig.settings = lib.mapAttrsRecursive (
        name: value: lib.mkDefault value
      ) cfg.nixConfig.settings;
      pynixd.builder.nixConfig.settings = lib.mapAttrsRecursive (
        name: value: lib.mkDefault value
      ) cfg.nixConfig.settings;
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
          "nix.conf" = builtins.readFile (cfg.pynixd.controller.nixConfig.nixConf);
          "logging.json" = builtins.toJSON cfg.loggingConfig;
        };
      };
    };
  };
}
