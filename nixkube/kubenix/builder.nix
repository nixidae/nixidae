# SPDX-License-Identifier: MIT

{
  config,
  lib,
  csiPkgs,
  curPkgs,
  ...
}:
let
  cfg = config.nixkube;
in
{
  options.nixkube.builder = {
    enable = (lib.mkEnableOption "ephemeral builder Job infrastructure (PodTemplate, ConfigMap)") // {
      default = true;
    };
    nixConfig = lib.mkOption {
      description = "nix.conf for builder pods";
      type = (import ./nixOptions.nix) curPkgs;
    };
  };

  config =
    let
      labels = cfg.labels // {
        "app.kubernetes.io/component" = "builder";
      };
    in
    lib.mkIf (cfg.enable && cfg.builder.enable) {
      nixkube.builder.nixConfig.settings = {
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
        # TODO: make configurable
        max-jobs = 5;
        builders-use-substitutes = true;
        narinfo-cache-negative-ttl = 0;
        narinfo-cache-positive-ttl = 0;
        warn-dirty = false;
        store = "daemon";
      };

      kubernetes.resources.${cfg.namespace} = {
        ConfigMap.builder = {
          metadata.labels = labels;
          data = {
            "nix.conf" = builtins.readFile cfg.builder.nixConfig.nixConf;
          };
        };

        PodTemplate.nixkube-builder = {
          metadata.labels = labels;
          template = {
            metadata.labels = labels;
            spec = {
              serviceAccountName = "nixkube";
              restartPolicy = "Never";
              containers = lib.mkNamedList {
                pynixd = {
                  command = [
                    "/nix/var/result/bin/tini"
                    "--"
                    "/nix/var/result/bin/pynixd-nixkube-builder"
                  ];
                  image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
                  env = lib.mkNamedList {
                    PYNIXD_SSH_HOST.value = "";
                    PYNIXD_SSH_PORT.value = "2222";
                    PYNIXD_HTTP_PORT.value = "8080";
                    PYNIXD_IDLE_TIMEOUT.value = toString cfg.pynixd.builderIdleTimeout;
                    HOME.value = "/nix/var/nix-csi/root";
                  };
                  ports = lib.mkNamedList {
                    ssh.containerPort = 2222;
                  };
                  volumeMounts = lib.mkNamedList {
                    nix-config.mountPath = "/etc/nix";
                    nix-store = {
                      mountPath = "/nix";
                      subPath = "nix";
                    };
                  };
                  resources = {
                    requests = {
                      memory = "256Mi";
                      cpu = "250m";
                    };
                  };
                };
              };
              volumes = lib.mkNamedList {
                nix-config.configMap.name = "builder";
                nix-store.csi = {
                  driver = "nixkube";
                  readOnly = false;
                  volumeAttributes = lib.mapAttrs (_: pkgs: pkgs.nixkube-pynixd-env) csiPkgs;
                };
              };
            };
          };
        };
      };
    };
}
