{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.nix-csi;

  defaultSystemFeatures = [
    "nixos-test"
    "benchmark"
    "big-parallel"
    # "kvm"
  ];

  semanticConfType =
    with lib.types;
    let
      confAtom =
        nullOr (oneOf [
          bool
          int
          float
          str
          path
          package
        ])
        // {
          description = "Nix config atom (null, bool, int, float, str, path or package)";
        };
    in
    attrsOf (either confAtom (listOf confAtom));

  nixSubmodule =
    with lib;
    types.submodule (
      { config, ... }:
      {
        options = {
          nixConf = mkOption {
            type = types.package;
            internal = true;
          };

          checkConfig = mkOption {
            type = types.bool;
            default = true;
            internal = true;
          };

          checkAllErrors = mkOption {
            type = types.bool;
            default = true;
            internal = true;
          };

          extraOptions = mkOption {
            description = "Extra lines to add to nix.conf";
            type = types.lines;
            default = "";
          };

          settings = mkOption {
            description = "Settings rendered to nix.conf";
            type = types.submodule {
              freeformType = semanticConfType;
              options = { };
            };
            default = { };
          };
        };
        config = {
          settings = {
            trusted-public-keys = [
              "cache.nixos.org-1:6NCHdD59X431o0gWypbMrAURkbJ16ZPMQFGspcDShjY="
              "nix-csi.cachix.org-1:i4w33gR4efO67jpz8U7g/MdvRQ6mQ3LEF9fB8tES60g="
            ];
            substituters = [
              "https://cache.nixos.org"
              "https://nix-csi.cachix.org"
            ];
            trusted-users = [ "root" ];
            system-features = defaultSystemFeatures;
          };
          nixConf =
            (pkgs.formats.nixConf {
              inherit (config)
                checkAllErrors
                checkConfig
                extraOptions
                ;
              package = pkgs.lixPackageSets.lix_2_93.lix.out;
              inherit (pkgs.lixPackageSets.lix_2_93.lix) version;
            }).generate
              "nix.conf"
              config.settings;
        };
      }
    );
in
{
  options.nix-csi.nixNodeConfig = lib.mkOption {
    description = "nix.conf for CSI/mounter/DaemonSet pods";
    type = nixSubmodule;
  };
  options.nix-csi.nixCacheConfig = lib.mkOption {
    description = "nix.conf for cache pod";
    type = nixSubmodule;
  };
  options.nix-csi.nixBuilderConfig = lib.mkOption {
    description = "nix.conf for builder pods";
    type = nixSubmodule;
  };

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
        nixNodeConfig.settings = sharedSettings // {
          keep-outputs = true; # Remove when we have separate builders
        };
        nixCacheConfig.settings = sharedSettings // {
          max-jobs = lib.mkDefault 0;
        };
        nixBuilderConfig.settings = sharedSettings // {
          max-jobs = "auto";
        };
      };
    kubernetes.resources.${cfg.namespace} = {
      ConfigMap.nix-node.data = {
        "nix.conf" = builtins.readFile (cfg.nixNodeConfig.nixConf);
        "logging.json" = builtins.toJSON cfg.loggingConfig;
      };
      ConfigMap.nix-cache.data = {
        "nix.conf" = builtins.readFile (cfg.nixCacheConfig.nixConf);
        "logging.json" = builtins.toJSON cfg.loggingConfig;
      };
      ConfigMap.nix-builder.data = {
        "nix.conf" = builtins.readFile (cfg.nixBuilderConfig.nixConf);
        "logging.json" = builtins.toJSON cfg.loggingConfig;
      };
    };
  };
}
