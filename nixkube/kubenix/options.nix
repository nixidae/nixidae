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
in
{
  options.nix-csi = {
    enable = lib.mkEnableOption "nix-csi";
    undeploy = lib.mkOption {
      type = lib.types.bool;
      default = false;
    };
    namespace = lib.mkOption {
      description = "Which namespace to deploy nix-csi to";
      type = lib.types.str;
      default = "nix-csi";
    };
    authorizedKeys = lib.mkOption {
      description = "SSH public keys that can connect to cache and builders";
      type = lib.types.listOf (lib.types.either lib.types.str lib.types.path);
      apply = lib.map (v: lib.trim (if lib.typeOf v == "path" then builtins.readFile v else v));
      default = [ ];
    };
    knownHosts = lib.mkOption {
      description = "SSH host keys to accept when connecting";
      type = lib.types.attrsOf (lib.types.either lib.types.str lib.types.path);
      apply = lib.mapAttrs (n: v: lib.trim (if lib.typeOf v == "path" then builtins.readFile v else v));
      default = { };
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
      description = "Where on the host to put nix-csi store, / is untested and not recommended";
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
    rsyncConcurrency = lib.mkOption {
      description = ''
        Maximum number of concurrent rsync operations when copying store paths.
        Higher values can improve performance but increase I/O load.
      '';
      type = lib.types.ints.positive;
      default = 1;
    };
    nodeBuildTimeout = lib.mkOption {
      description = ''
        Timeout in seconds for Nix build operations on node pods.
        Builds exceeding this timeout will be terminated.
      '';
      type = lib.types.ints.positive;
      default = 300; # 5 minutes
    };
    loggingConfig = lib.mkOption {
      description = ''
        Python logging configuration dict for nix-csi service.
        See https://docs.python.org/3/library/logging.config.html#logging-config-dictschema
      '';
      type = (pkgs.formats.json { }).type;
      default = {
        version = 1;
        formatters = {
          standard = {
            format = "%(asctime)s %(levelname)s [%(name)s] %(message)s";
          };
        };
        handlers = {
          console = {
            class = "logging.StreamHandler";
            formatter = "standard";
            stream = "ext://sys.stdout";
          };
        };
        loggers = {
          nix-csi = {
            level = "INFO";
            handlers = [ "console" ];
            propagate = false;
          };
          httpx = {
            level = "WARNING";
            handlers = [ "console" ];
            propagate = false;
          };
        };
        root = {
          level = "WARN";
          handlers = [ "console" ];
        };
      };
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
              nix-csi-builder-env = pkgs.callPackage ../environments/builder {
                inherit (cfg) dinix;
              };
            })
          ];
        };

      # If we haven't set cfg.push we discard context, this means we don't build the package locally, just render the storePath
      # this works as long as you evaluate to something that's passed through CI and pushed to nix-csi cachix.
      maybePush = pkg: if cfg.push then pkg else builtins.unsafeDiscardStringContext pkg;

    in
    lib.mkIf cfg.enable {
      # Provide helpers to all modules via _module.args
      _module.args = {
        inherit maybePush;
        x86Pkgs = mkPkgs "x86_64-linux";
        armPkgs = mkPkgs "aarch64-linux";
      };

      nix-csi = { };
    };
}
