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
  nsRes = config.kubernetes.resources.${cfg.namespace};
in
{
  options.nixkube.pynixd = {
    enable =
      (lib.mkEnableOption "pynixd StatefulSet (shared Nix binary cache and build distributor)")
      // {
        default = true;
      };
    nixConfig = lib.mkOption {
      description = "nix.conf for pynixd pod";
      type = (import ./nixOptions.nix) curPkgs;
    };

    storageClassName = lib.mkOption {
      description = "StorageClass for the pynixd PVC. null uses the cluster's default StorageClass.";
      type = lib.types.nullOr lib.types.str;
      default = null;
      example = "fast-ssd";
    };
    loadBalancerPort = lib.mkOption {
      description = ''
        External SSH port for the pynixd LoadBalancer Service.
        Set to null to disable the LoadBalancer (cluster-internal access only).
      '';
      type = lib.types.nullOr lib.types.int;
      default = 2222;
    };
  };
  config =
    let
      labels = cfg.labels // {
        "app.kubernetes.io/component" = "pynixd";
      };
      matchLabels = cfg.matchLabels // {
        "app.kubernetes.io/component" = "pynixd";
      };
    in
    lib.mkIf (cfg.enable && cfg.pynixd.enable) {
      kubernetes.resources.${cfg.namespace} = {
        StatefulSet.pynixd = {
          metadata.labels = labels;
          metadata.annotations."nixkube/discard" = "true";
          spec = {
            serviceName = "pynixd";
            replicas = 1;
            selector.matchLabels = labels;
            template = {
              metadata.labels = labels;
              metadata.annotations = {
                "kubectl.kubernetes.io/default-container" = "pynixd";
                configHash = lib.hashAttrs (
                  { } // nsRes.ConfigMap.pynixd or { } // nsRes.ConfigMap.ssh-config or { }
                );
              };
              spec = {
                serviceAccountName = "nixkube";
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
                        readOnly = true;
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
                  pynixd = {
                    command = [
                      "tini"
                      "--"
                      "pynixd-nixkube"
                    ];
                    image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
                    env = lib.mkNamedList {
                      PYNIXD_ENABLED.value = lib.boolToString cfg.pynixd.enable;
                      PYNIXD_SSH_HOST.value = "";
                      PYNIXD_SSH_PORT.value = "2222";
                      PYNIXD_HTTP_PORT.value = "8080";
                      PYNIXD_SSH_HOST_KEY.value = "/nix/var/pynixd/host_key";
                      HOME.value = "/nix/var/nix-csi/root";
                      KUBE_NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                    };
                    ports = lib.mkNamedList {
                      ssh.containerPort = 2222;
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
                  nix-config.configMap.name = "pynixd";
                  nix-key.secret.secretName = "nix-key";
                  init-store.csi = {
                    driver = "nixkube";
                    readOnly = true;
                    volumeAttributes = lib.mapAttrs (_: pkgs: pkgs.nixkube-pynixd-env) csiPkgs;
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
                  inherit (cfg.pynixd) storageClassName;
                };
              };
            };
          };
        };

        Service.pynixd = {
          metadata.labels = labels;
          spec = {
            selector = matchLabels;
            ports = lib.mkNamedList {
              ssh = {
                port = 2222;
                targetPort = "ssh";
              };
            };
            type = "ClusterIP";
          };
        };
        Service.pynixd-lb = lib.mkIf (cfg.pynixd.loadBalancerPort != null) {
          metadata.labels = labels;
          spec = {
            selector = matchLabels;
            ports = lib.mkNamedList {
              ssh = {
                port = cfg.pynixd.loadBalancerPort;
                targetPort = "ssh";
              };
            };
            type = "LoadBalancer";
          };
        };
      };
    };
}
