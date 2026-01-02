{
  config,
  pkgs,
  lib,
  ...
}:
let
  cfg = config.nix-csi;
  inputs =
    (
      let
        lockFile = builtins.readFile ../flake.lock;
        lockAttrs = builtins.fromJSON lockFile;
        fcLockInfo = lockAttrs.nodes.flake-compatish.locked;
        fcSrc = builtins.fetchTree fcLockInfo;
        flake-compatish = import fcSrc;
      in
      flake-compatish ../.
    ).inputs;

  keyDrv =
    pkgs.runCommand "nix-csi-ssh-keys"
      {
        nativeBuildInputs = [ pkgs.openssh ];
      }
      ''
        mkdir -p $out
        ssh-keygen -t ed25519 -N "" -f $out/id_ed25519 -C "nix-csi-fallback-insecure"
      '';
in
{
  options.nix-csi = {
    enable = lib.mkEnableOption "nix-csi";
    undeploy = lib.mkOption {
      type = lib.types.bool;
      default = false;
    };
    namespace = lib.mkOption {
      description = "Which namespace to deploy cknix resources too";
      type = lib.types.str;
      default = "nix-csi";
    };
    authorizedKeys = lib.mkOption {
      description = "SSH public keys that can connect to cache and builders";
      type = lib.types.listOf lib.types.str;
      default = [ ];
    };
    pubKey = lib.mkOption {
      description = "Public SSH key used for in-cluster SSH communication";
      type = lib.types.str;
      default = builtins.readFile "${keyDrv}/id_ed25519.pub";
    };
    privKey = lib.mkOption {
      description = "Private SSH key used for in-cluster SSH communication";
      type = lib.types.str;
      default = builtins.readFile "${keyDrv}/id_ed25519";
    };
    version = lib.mkOption {
      type = lib.types.str;
      default =
        let
          pyproject = builtins.fromTOML (builtins.readFile ../python/pyproject.toml);
        in
        pyproject.project.version;
    };
    hostMountPath = lib.mkOption {
      description = "Where on the host to put cknix store";
      type = lib.types.path;
      default = "/var/lib/nix-csi";
    };
    internalServiceName = lib.mkOption {
      description = ''
        Internal service name used for reaching builder nodes from cache node
      '';
      type = lib.types.str;
      default = "nix-builders";
    };

    pkgs = lib.mkOption {
      type = lib.types.path;
      default = inputs.nixpkgs;
      internal = true;
    };
    push = lib.mkOption {
      type = lib.types.bool;
      internal = true;
      default = false;
    };
    dinix = lib.mkOption {
      type = lib.types.path;
      internal = true;
      default = inputs.dinix;
    };
  };
  config =
    let
      mkPkgs =
        system:
        import cfg.pkgs {
          inherit system;
          overlays = [
            (import ../pkgs)
            (self: pkgs: {
              nix-csi-node-env = pkgs.callPackage ../environments/node {
                inherit (cfg) dinix;
              };
              nix-csi-cache-env = pkgs.callPackage ../environments/cache {
                inherit (cfg) dinix;
              };
            })
          ];
        };

      maybePush = pkg: if cfg.push then pkg else builtins.unsafeDiscardStringContext pkg;

    in
    lib.mkIf cfg.enable {
      # Provide helpers to all modules via _module.args
      _module.args = {
        inherit maybePush;
        x86Pkgs = mkPkgs "x86_64-linux";
        armPkgs = mkPkgs "aarch64-linux";
      };

      nix-csi.authorizedKeys = [ cfg.pubKey ];
    };
}
