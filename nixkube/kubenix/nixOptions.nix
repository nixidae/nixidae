pkgs:
let
  inherit (pkgs) lib;
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
in
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
          # Use normal lix so we don't have to build lruLix locally
          package = pkgs.lixPackageSets.lix_2_94.lix.out;
          inherit (pkgs.lixPackageSets.lix_2_94.lix) version;
        }).generate
          "nix.conf"
          config.settings;
    };
  }
)
