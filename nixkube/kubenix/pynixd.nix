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

  # Shared across pynixd central and builders
  image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
  storeVolumeAttributes = lib.mapAttrs (_: pkgs: pkgs.nixkube-pynixd-env) csiPkgs;

  pynixdLabels = cfg.labels // {
    "app.kubernetes.io/component" = "pynixd";
  };
  pynixdMatchLabels = cfg.matchLabels // {
    "app.kubernetes.io/component" = "pynixd";
  };
  builderLabels = cfg.labels // {
    "app.kubernetes.io/component" = "builder";
  };

  # Only enabled systems
  enabledSystems = lib.filterAttrs (_: v: v) cfg.systems;
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
      type = (import ./nixOptions.nix) {
        pkgs = curPkgs;
        nix = config.nixkube.nix.package;
      };
    };
    authorizedKeys = lib.mkOption {
      description = "SSH public keys that can connect to cache. Used by nodes to push built store paths to the cache.";
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
    builderMax = lib.mkOption {
      description = "Maximum number of ephemeral builder Jobs that pynixd can create.";
      type = lib.types.ints.positive;
      default = 3;
    };
    builderMin = lib.mkOption {
      description = "Minimum number of builder Jobs to keep alive even when idle.";
      type = lib.types.ints.unsigned;
      default = 1;
    };
    builderIdleTimeout = lib.mkOption {
      description = "Seconds of inactivity before an ephemeral builder pod shuts down.";
      type = lib.types.ints.positive;
      default = 300;
    };
    builder = {
      nixConfig = lib.mkOption {
        description = "nix.conf for builder pods";
        type = (import ./nixOptions.nix) {
          pkgs = curPkgs;
          nix = config.nixkube.nix.package;
        };
      };
    };
  };
  config = lib.mkIf (cfg.enable && cfg.pynixd.enable) {
    nixkube.pynixd.builder.nixConfig.settings = {
      max-jobs = 5;
      warn-dirty = false;
    };

    kubernetes.resources.${cfg.namespace} = {
      StatefulSet.pynixd = {
        metadata.labels = pynixdLabels;
        metadata.annotations."nixkube/discard" = "true";
        spec = {
          serviceName = "pynixd";
          replicas = 1;
          selector.matchLabels = pynixdLabels;
          template = {
            metadata.labels = pynixdLabels;
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
                  inherit image;
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
                    "pynixd-nixkube-central"
                  ];
                  inherit image;
                  env = lib.mkNamedList {
                    PYNIXD_ENABLED.value = lib.boolToString cfg.pynixd.enable;
                    PYNIXD_SSH_HOST.value = "";
                    PYNIXD_SSH_PORT.value = "22";
                    PYNIXD_HTTP_PORT.value = "8080";
                    PYNIXD_SSH_HOST_KEY.value = "/nix/var/pynixd/host_key";
                    HOME.value = "/nix/var/nix-csi/root";
                    PYNIXD_KUBE_NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                    PYNIXD_BUILDER_MAX.value = toString cfg.pynixd.builderMax;
                    PYNIXD_BUILDER_MIN.value = toString cfg.pynixd.builderMin;
                    PYNIXD_IDLE_TIMEOUT.value = toString cfg.pynixd.builderIdleTimeout;
                    PYNIXD_SCHEDULE_MODE.value = "scheduler";
                    PYNIXD_SYSTEMS.value = lib.concatStringsSep "," (builtins.attrNames enabledSystems);
                  };
                  ports = lib.mkNamedList {
                    ssh.containerPort = 22;
                  };
                  readinessProbe.tcpSocket.port = "ssh";
                  livenessProbe.tcpSocket.port = "ssh";
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
                  volumeAttributes = storeVolumeAttributes;
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
        metadata.labels = pynixdLabels;
        spec = {
          selector = pynixdMatchLabels;
          ports = lib.mkNamedList {
            ssh = {
              port = 22;
              targetPort = "ssh";
            };
          };
          type = "ClusterIP";
        };
      };
      Service.pynixd-lb = lib.mkIf (cfg.pynixd.loadBalancerPort != null) {
        metadata.labels = pynixdLabels;
        spec = {
          selector = pynixdMatchLabels;
          ports = lib.mkNamedList {
            ssh = {
              port = cfg.pynixd.loadBalancerPort;
              targetPort = "ssh";
            };
          };
          type = "LoadBalancer";
        };
      };

      ConfigMap.builder = {
        metadata.labels = builderLabels;
        data = {
          "nix.conf" = builtins.readFile cfg.pynixd.builder.nixConfig.nixConf;
        };
      };

      PodTemplate.nixkube-builder = {
        metadata.labels = builderLabels;
        template = {
          metadata.labels = builderLabels;
          spec = {
            serviceAccountName = "nixkube";
            restartPolicy = "Never";
            affinity = {
              podAntiAffinity = {
                preferredDuringSchedulingIgnoredDuringExecution = [
                  {
                    weight = 100;
                    podAffinityTerm = {
                      topologyKey = "kubernetes.io/hostname";
                      labelSelector.matchLabels = builderLabels;
                    };
                  }
                ];
              };
            };
            containers = lib.mkNamedList {
              pynixd = {
                command = [
                  "/nix/var/result/bin/tini"
                  "--"
                  "/nix/var/result/bin/pynixd-nixkube-builder"
                ];
                inherit image;
                env = lib.mkNamedList {
                  PYNIXD_SSH_HOST.value = "";
                  PYNIXD_SSH_PORT.value = "22";
                  PYNIXD_HTTP_PORT.value = "8080";
                  PYNIXD_IDLE_TIMEOUT.value = toString cfg.pynixd.builderIdleTimeout;
                  HOME.value = "/nix/var/nix-csi/root";
                };
                ports = lib.mkNamedList {
                  ssh.containerPort = 22;
                };
                readinessProbe.tcpSocket.port = "ssh";
                livenessProbe.tcpSocket.port = "ssh";
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
                volumeAttributes = storeVolumeAttributes;
              };
            };
          };
        };
      };
    };
  };
}
