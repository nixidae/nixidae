# SPDX-License-Identifier: MIT

{
  config,
  curPkgs,
  lib,
  ...
}:
let
  cfg = config.nixkube;
  defaultLoggers = {
    "nixkube".level = "INFO";
    "nixkube.nix_daemon".level = "WARNING";
    "httpx".level = "WARNING";
  };
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
  imports = [
    (lib.mkRenamedOptionModule [ "nix-csi" ] [ "nixkube" ])
  ];
  options.nixkube = {
    enable = lib.mkEnableOption "nixkube";
    undeploy = lib.mkOption {
      description = "When true, removes all nixkube Kubernetes resources on the next apply.";
      type = lib.types.bool;
      default = false;
    };
    deploySecrets = lib.mkOption {
      description = "Deploy SSH keypair Secrets to Kubernetes. Disable if managing secrets externally (e.g., with Vault or Sealed Secrets).";
      type = lib.types.bool;
      default = true;
    };
    namespace = lib.mkOption {
      description = "Which namespace to deploy nixkube to";
      type = lib.types.str;
      default = "nixkube";
    };
    authorizedKeys = lib.mkOption {
      description = "SSH public keys that can connect to cache and builders. Used by nodes to push built store paths to the cache.";
      type = lib.types.listOf (lib.types.either lib.types.str lib.types.path);
      apply = lib.map (v: lib.trim (if lib.typeOf v == "path" then builtins.readFile v else v));
      default = [ ];
      example = lib.literalExpression ''
        [
          "ssh-ed25519 AAAA... user@host"
          ./keys/deploy.pub
        ]
      '';
    };
    knownHosts = lib.mkOption {
      description = ''
        SSH host keys to accept when connecting to cache and builders.
        Keys are written to known_hosts on nodes so they can connect without interactive verification.
      '';
      type = lib.types.attrsOf (lib.types.either lib.types.str lib.types.path);
      apply = lib.mapAttrs (n: v: lib.trim (if lib.typeOf v == "path" then builtins.readFile v else v));
      default = { };
      example = lib.literalExpression ''
        {
          "nix-cache" = "ssh-ed25519 AAAA...";
        }
      '';
    };
    metadata = lib.mkOption {
      description = "Metadata (labels, annotations) applied to nixkube resources";
      type = (curPkgs.formats.json { }).type;
      default = { };
    };
    version = lib.mkOption {
      internal = true;
      type = lib.types.str;
      default =
        let
          pyproject = builtins.fromTOML (builtins.readFile ../pkgs/nixkube/pyproject.toml);
        in
        pyproject.project.version;
    };
    hostMountPath = lib.mkOption {
      description = "Where on the host to put nixkube store, / is untested and not recommended";
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
    nodeBuildTimeout = lib.mkOption {
      description = ''
        Timeout in seconds for Nix build operations on node pods.
        Builds exceeding this timeout will be terminated.
      '';
      type = lib.types.ints.positive;
      default = 300; # 5 minutes
    };
    loggingConfig = lib.mkOption {
      description = "Logging configuration for the nixkube service (structlog-based).";
      default = { };
      type = lib.types.submodule {
        options = {
          renderer = lib.mkOption {
            type = lib.types.enum [
              "json"
              "logfmt"
              "console"
            ];
            default = "json";
            description = ''
              Log output renderer:

              - `"json"` (default): Structured JSON, one object per line. Recommended
                for production and log aggregation (Loki, ELK, Datadog). Each
                structured field is a top-level JSON key, enabling rich queries:
                ```
                {app="nixkube"} | json | elapsed_time > 10
                {app="nixkube"} | json | returncode != 0
                {app="nixkube"} | json | container_id =~ "abc"
                ```

              - `"logfmt"`: `key=value` pairs on a single line. Human-readable and
                machine-parseable. Works well with `stern`, `kubectl logs | grep`,
                and log shippers with native logfmt support (Vector, Fluentd).
                Example line:
                ```
                level=info logger=nixkube.nri event=build_task_completed container_id=abc123
                ```

              - `"console"`: Coloured, aligned output for local development.
                Not suitable for log aggregation or machine parsing.
            '';
            example = "logfmt";
          };
          loggers = lib.mkOption {
            type = lib.types.attrsOf (
              lib.types.submodule {
                options.level = lib.mkOption {
                  type = lib.types.enum [
                    "DEBUG"
                    "INFO"
                    "WARNING"
                    "ERROR"
                    "CRITICAL"
                  ];
                  description = "Log level for this logger.";
                };
              }
            );
            default = defaultLoggers;
            description = ''
              Per-logger level overrides. Keys are Python logger names (dotted hierarchy).
              All loggers under `nixkube.*` inherit from `nixkube` unless individually overridden.
            '';
            example = lib.literalExpression ''
              {
                "nixkube".level = "DEBUG";
                "nixkube.nri".level = "DEBUG";
                "httpx".level = "ERROR";
              }
            '';
          };
          root = lib.mkOption {
            description = "Root logger configuration (catch-all for third-party libraries).";
            default = { };
            type = lib.types.submodule {
              options.level = lib.mkOption {
                type = lib.types.enum [
                  "DEBUG"
                  "INFO"
                  "WARNING"
                  "ERROR"
                  "CRITICAL"
                ];
                default = "WARNING";
                description = "Root logger level. All loggers inherit this unless overridden in `loggers`.";
              };
            };
          };
        };
      };
      example = lib.literalExpression ''
        # JSON renderer (default) — production/Loki
        {
          renderer = "json";
          loggers.nixkube.level = "DEBUG";
          root.level = "WARNING";
        }

        # Logfmt renderer — stern / grep-friendly
        {
          renderer = "logfmt";
          loggers.nixkube.level = "INFO";
        }

        # Console renderer — local development
        {
          renderer = "console";
          loggers.nixkube.level = "DEBUG";
          root.level = "DEBUG";
        }
      '';
    };
    systems = lib.mkOption {
      description = ''
        Which CPU architectures to build nixkube environments for.
        Disable aarch64-linux to skip cross-compilation if your cluster is x86_64-only.
      '';
      type = lib.types.attrsOf lib.types.bool;
      default = {
        "x86_64-linux" = true;
        "aarch64-linux" = true;
      };
      example = lib.literalExpression ''
        {
          "x86_64-linux" = true;
          "aarch64-linux" = false;
        }
      '';
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
      description = "All nixkube labels including version (for metadata.labels)";
    };
    matchLabels = lib.mkOption {
      type = lib.types.attrsOf lib.types.str;
      internal = true;
      description = "nixkube base labels without version (for selector.matchLabels)";
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
                nixkube-node-env = callPackage ../environments/node;
                nixkube-cache-env = callPackage ../environments/cache;
                nixkube-builder-env = callPackage ../environments/builder;
                nixkube-proxy-env = callPackage ../environments/proxy;
              }
            )
          ];
        };
    in
    lib.mkIf cfg.enable {
      # Provide helpers to all modules via _module.args
      _module.args = {
        csiPkgs = lib.pipe cfg.systems [
          (lib.filterAttrs (_: enabled: enabled))
          (lib.mapAttrs (system: _: mkPkgs system))
        ];
        curPkgs = mkPkgs builtins.currentSystem;
        subPath = spath: lib.removePrefix "/" (toString spath);
      };

      # Set default loggers
      nixkube.loggingConfig.loggers = lib.mapAttrsRecursive (_: v: lib.mkDefault v) defaultLoggers;

      # Set internal label options using the derived values
      nixkube.matchLabels = {
        "app.kubernetes.io/name" = "nixkube";
        "app.kubernetes.io/part-of" = "nixkube";
        "app.kubernetes.io/managed-by" = "nix";
      };
      nixkube.labels = cfg.matchLabels // {
        "app.kubernetes.io/version" = cfg.version;
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
        if resource.metadata.annotations."nixkube/discard" or null != null then
          mapRecursive (x: if lib.isString x then builtins.unsafeDiscardStringContext x else x) resource
        else
          resource
      );
    };
}
