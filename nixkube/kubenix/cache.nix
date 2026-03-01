# SPDX-License-Identifier: MIT

{
  config,
  lib,
  x86Pkgs,
  armPkgs,
  curPkgs,
  ...
}:
let
  cfg = config.nixkube;
  nsRes = config.kubernetes.resources.${cfg.namespace};
in
{
  options.nixkube.cache = {
    enable = (lib.mkEnableOption "cache") // {
      default = true;
    };
    nixConfig = lib.mkOption {
      description = "nix.conf for cache pod";
      type = (import ./nixOptions.nix) curPkgs;
    };

    storageClassName = lib.mkOption {
      description = "Which SC to use, defaults to null which will use default SC";
      type = lib.types.nullOr lib.types.str;
      default = null;
    };
    loadBalancerPort = lib.mkOption {
      description = "Port to run public SSH on for Nix cache";
      type = lib.types.nullOr lib.types.int;
      default = 2222;
    };
  };
  config =
    let
      labels = cfg.labels // {
        "app.kubernetes.io/component" = "cache";
      };
      matchLabels = cfg.matchLabels // {
        "app.kubernetes.io/component" = "cache";
      };
    in
    lib.mkIf (cfg.enable && cfg.cache.enable) {
      kubernetes.resources.${cfg.namespace} = {
        StatefulSet.nix-cache = {
          metadata.labels = labels;
          spec = {
            serviceName = "nix-cache";
            replicas = 1;
            selector.matchLabels = labels;
            template = {
              metadata.labels = labels;
              metadata.annotations = {
                "kubectl.kubernetes.io/default-container" = "nix-cache";
                "nix-csi/discard" = "true";
                configHash = lib.hashAttrs (
                  { } // nsRes.ConfigMap.nix-cache or { } // nsRes.ConfigMap.ssh-config or { }
                );
              };
              spec = {
                serviceAccountName = "nix-csi";
                priorityClassName = "system-cluster-critical";
                initContainers = lib.mkNumberedList {
                  "1" = {
                    name = "initcopy";
                    image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
                    command = [ "initCopy" ];
                    imagePullPolicy = "Always";
                    securityContext.capabilities.add = [ "SYS_CHROOT" ]; # chroot store
                    volumeMounts = lib.mkNamedList {
                      init-store = {
                        mountPath = "/nix";
                        subPath = "nix";
                      };
                      nix-store.mountPath = "/nix-volume";
                      nix-config.mountPath = "/etc/nix";

                      ssh-config.mountPath = "/etc/ssh";
                      ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                      ssh-key.mountPath = "/etc/ssh-key";
                    };
                    resources = {
                      requests = {
                        memory = "64Mi";
                        cpu = "100m";
                      };
                    };
                  };
                };
                containers = lib.mkNamedList {
                  nix-cache = {
                    command = [
                      "dinit"
                      "--log-file"
                      "/var/log/dinit.log"
                      "--quiet"
                      "cache"
                    ];
                    image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
                    env = lib.mkNamedList {
                      BUILDERS_ENABLED.value = lib.boolToString cfg.builders.enable;
                      CACHE_ENABLED.value = lib.boolToString cfg.cache.enable; # copy to itself is a bit weird?
                      IS_CACHE.value = lib.boolToString true;
                      GC_KEEP_SECONDS.value = "86400";
                      HOME.value = "/nix/var/nix-csi/root";
                      KUBE_NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                    };
                    ports = lib.mkNamedList {
                      ssh.containerPort = 22;
                    };
                    volumeMounts = lib.mkNamedList {
                      nix-config.mountPath = "/etc/nix";
                      nix-key.mountPath = "/etc/nix-key";
                      nix-store = {
                        mountPath = "/nix";
                        subPath = "nix";
                      };

                      ssh-config.mountPath = "/etc/ssh";
                      ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                      ssh-key.mountPath = "/etc/ssh-key";
                    };
                    resources = {
                      requests = {
                        memory = "64Mi";
                        cpu = "100m";
                      };
                    };
                  };
                };
                volumes = lib.mkNamedList {
                  nix-config.configMap.name = "nix-cache";
                  nix-key.secret.secretName = "nix-key";
                  init-store.csi = {
                    driver = "nixkube";
                    readOnly = true;
                    volumeAttributes = {
                      # Only render storePaths here, building is done with a ConfigMap (config.nix) only if cfg.push is set
                      # this is so users don't have to build locally to deploy.
                      x86_64-linux = x86Pkgs.nix-csi-cache-env;
                      aarch64-linux = armPkgs.nix-csi-cache-env;
                    };
                  };

                  ssh-config.configMap = {
                    name = "ssh-config";
                    defaultMode = 292; # 444
                  };
                  ssh-dynauth.configMap = {
                    name = "ssh-dynauth";
                    defaultMode = 292; # 444
                  };
                  ssh-key.secret = {
                    secretName = "ssh-key";
                    defaultMode = 256; # 400
                  };
                };
              };
            };
            volumeClaimTemplates = lib.mkNumberedList {
              "1" = {
                metadata.name = "nix-store";
                spec = {
                  accessModes = [ "ReadWriteOnce" ];
                  resources.requests.storage = "10Gi";
                  inherit (cfg.cache) storageClassName;
                };
              };
            };
          };
        };

        Service.nix-cache = {
          metadata.labels = labels;
          spec = {
            selector = matchLabels;
            ports = lib.mkNamedList {
              ssh = {
                port = 22;
                targetPort = "ssh";
              };
            };
            type = "ClusterIP";
          };
        };
        Service.nix-cache-lb = lib.mkIf (cfg.cache.loadBalancerPort != null) {
          metadata.labels = labels;
          spec = {
            selector = matchLabels;
            ports = lib.mkNamedList {
              ssh = {
                port = cfg.cache.loadBalancerPort;
                targetPort = "ssh";
              };
            };
            type = "LoadBalancer";
          };
        };
      };
    };
}
