# SPDX-License-Identifier: MIT

{
  config,
  curPkgs,
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
      flake-compatish {
        source = ../.;
        overrides = {
          self = ../.;
        };
      }
    ).inputs;
in
{
  options.nix-csi = {
    enable = lib.mkEnableOption "nix-csi";
    undeploy = lib.mkOption {
      type = lib.types.bool;
      default = false;
    };
    deploySecrets = lib.mkOption {
      type = lib.types.bool;
      default = true;
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
    metadata = lib.mkOption {
      description = "Metadata (labels, annotations) applied to nix-csi resources";
      type = (curPkgs.formats.json { }).type;
      default = { };
    };
    version = lib.mkOption {
      type = lib.types.str;
      default =
        let
          pyproject = builtins.fromTOML (builtins.readFile ../pkgs/nixkube/pyproject.toml);
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
        Merged with built-in defaults, so you only need to override specific parts.
        See https://docs.python.org/3/library/logging.config.html#logging-config-dictschema
      '';
      type = lib.types.attrsOf (curPkgs.formats.json { }).type;
      default = { };
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
    labels = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      internal = true;
      description = "All nix-csi labels including version (for metadata.labels)";
    };
    matchLabels = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      internal = true;
      description = "nix-csi base labels without version (for selector.matchLabels)";
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
            (
              final: prev:
              let
                callPackage =
                  packagePath:
                  final.callPackage packagePath {
                    inherit (cfg) dinix;
                  };
              in
              {
                nix-csi-node-env = callPackage ../environments/node;
                nix-csi-cache-env = callPackage ../environments/cache;
                nix-csi-builder-env = callPackage ../environments/builder;
                nix-csi-proxy-env = callPackage ../environments/proxy;
              }
            )
          ];
        };
    in
    lib.mkIf cfg.enable {
      # Provide helpers to all modules via _module.args
      _module.args = {
        x86Pkgs = mkPkgs "x86_64-linux";
        armPkgs = mkPkgs "aarch64-linux";
        curPkgs = mkPkgs builtins.currentSystem;
        subPath = spath: lib.removePrefix "/" (toString spath);
      };

      # Set internal label options using the derived values
      nix-csi.matchLabels = {
        "app.kubernetes.io/name" = "nix-csi";
        "app.kubernetes.io/part-of" = "nix-csi";
        "app.kubernetes.io/managed-by" = "nix";
      };
      nix-csi.labels = cfg.matchLabels // {
        "app.kubernetes.io/version" = cfg.version;
      };

      nix-csi.loggingConfig = lib.mapAttrsRecursive (_: v: lib.mkDefault v) {
        version = 1;
        formatters = {
          standard = {
            format = "%(levelname)s [%(name)s] %(message)s";
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
          nix-csi.level = "INFO";
          nix-nri.level = "INFO";
          httpx.level = "WARNING";
        };
        root = {
          level = "WARN";
          handlers = [ "console" ];
        };
      };

      kubernetes.transformers = lib.optional (!cfg.push) (
        resource:
        let
          mapRecursive =
            f: value:
            if builtins.isAttrs value then
              builtins.mapAttrs (n: v: mapRecursive f v) value
            else if builtins.isList value then
              map (mapRecursive f) value
            else
              f value;
        in
        if resource.metadata.annotations."nix-csi/discard" or null != null then
          mapRecursive (x: if lib.isString x then builtins.unsafeDiscardStringContext x else x) resource
        else
          resource
      );
    };
}
