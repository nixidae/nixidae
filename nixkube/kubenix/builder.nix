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
  options.nixkube.builders =
    let
      deployType = lib.types.submodule (
        { ... }:
        {
          options = {
            enable = (lib.mkEnableOption "builder pods") // {
              default = cfg.builders.enable;
            };
            replicas = lib.mkOption {
              description = "Number of builder pod replicas";
              type = lib.types.ints.positive;
              default = 1;
            };
            arch = lib.mkOption {
              description = "GOARCH / kubernetes.io/arch to deploy to";
              type = lib.types.nonEmptyStr;
              default = "amd64";
            };
            labels = lib.mkOption {
              description = "Pod labels";
              type = lib.types.attrsOf lib.types.str;
              default = { };
            };
            resources = lib.mkOption {
              description = "Resource requests/limits for builder pods";
              type = (curPkgs.formats.json { }).type;
              default = {
                requests = {
                  cpu = "1";
                  memory = "2Gi";
                  ephemeral-storage = "5Gi";
                };
                limits = {
                  ephemeral-storage = "5Gi";
                };
              };
            };
          };
        }
      );
    in
    {
      enable = (lib.mkEnableOption "builder pods") // {
        default = true;
      };
      loadBalancerPort = lib.mkOption {
        description = "Port to run public SSH on for builder jumpbox";
        type = lib.types.nullOr lib.types.ints.positive;
        default = 2223;
      };
      privilegedSandboxedBuilds = lib.mkOption {
        description = "To set up the sandbox Nix must run with privileges, without the sandbox Nix builds can run unprivileged";
        type = lib.types.bool;
        default = true;
      };
      nixConfig = lib.mkOption {
        description = "nix.conf for builder pods";
        type = (import ./nixOptions.nix) curPkgs;
      };
      deployments = lib.mkOption {
        type = lib.types.attrsOf deployType;
        default = { };
      };
      daemonsets = lib.mkOption {
        type = lib.types.attrsOf deployType;
        default = { };
      };
    };
  config =
    let
      labels = cfg.labels // {
        "app.kubernetes.io/component" = "builder";
      };
      matchLabels = cfg.matchLabels // {
        "app.kubernetes.io/component" = "builder";
      };
    in
    lib.mkIf (cfg.enable && cfg.builders.enable) {
      nixkube.builders.nixConfig.settings.sandbox = cfg.builders.privilegedSandboxedBuilds;

      kubernetes.resources.${cfg.namespace} =
        let
          podTemplate = v: {
            metadata.labels = v.labels;
            metadata.annotations = {
              "kubectl.kubernetes.io/default-container" = "nix-builder";
              "nix-csi/discard" = "true";
              configHash = lib.hashAttrs (
                { } // nsRes.ConfigMap.nix-builder or { } // nsRes.ConfigMap.ssh-config or { }
              );
            };
            spec = {
              nodeSelector."kubernetes.io/arch" = v.arch;
              priorityClassName = "system-cluster-critical";
              serviceAccountName = "nixkube";
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
                };
              };
              containers = lib.mkNamedList {
                nix-builder = {
                  command = [
                    "dinit"
                    "--log-file"
                    "/var/log/dinit.log"
                    "--quiet"
                    "builder"
                  ];
                  image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
                  env = lib.mkNamedList {
                    BUILDERS_ENABLED.value = lib.boolToString cfg.builders.enable;
                    CACHE_ENABLED.value = lib.boolToString cfg.cache.enable;
                    HOME.value = "/nix/var/nix-csi/root";
                    KUBE_NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                  };
                  resources = v.resources;
                  volumeMounts = lib.mkNamedList {
                    nix-config.mountPath = "/etc/nix";
                    nix-store = {
                      mountPath = "/nix";
                      subPath = "nix";
                    };

                    ssh-config.mountPath = "/etc/ssh";
                    ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                    ssh-key.mountPath = "/etc/ssh-key";
                  };
                  securityContext.privileged = cfg.builders.privilegedSandboxedBuilds;
                };
              };
              volumes = lib.mkNamedList {
                nix-config.configMap.name = "nix-builder";
                init-store.csi = {
                  driver = "nixkube";
                  readOnly = true;
                  volumeAttributes = {
                    # Only render storePaths here, building is done with a ConfigMap (config.nix) only if cfg.push is set
                    # this is so users don't have to build locally to deploy.
                    x86_64-linux = x86Pkgs.nixkube-builder-env;
                    aarch64-linux = armPkgs.nixkube-builder-env;
                  };
                };
                nix-store.emptyDir = { };

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
        in
        {
          Deployment =
            (lib.mapAttrs (
              n: v:
              let
                v2 = lib.recursiveUpdate v {
                  labels = labels // {
                    "kubernetes.io/arch" = v.arch;
                    "nix.csi/deployment" = n;
                  };
                };
              in
              {
                metadata.labels = v2.labels;
                spec = {
                  replicas = v.replicas;
                  selector.matchLabels = cfg.matchLabels // {
                    "app.kubernetes.io/component" = "builder";
                    "kubernetes.io/arch" = v.arch;
                    "nix.csi/deployment" = n;
                  };
                  template = podTemplate v2;
                };
              }
            ) (lib.filterAttrs (n: v: v.enable) cfg.builders.deployments))
            // {
              proxy = lib.mkIf (cfg.builders.loadBalancerPort != null) (
                let
                  labels = cfg.labels // {
                    "app.kubernetes.io/component" = "proxy";
                  };
                in
                {
                  metadata.labels = labels;
                  spec = {
                    replicas = 1;
                    selector.matchLabels = cfg.matchLabels // labels;
                    template = {
                      metadata.labels = labels;
                      metadata.annotations = {
                        "kubectl.kubernetes.io/default-container" = "proxy";
                        "nix-csi/discard" = "true";
                      };
                      spec = {
                        containers = lib.mkNamedList {
                          proxy = {
                            image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
                            command = [
                              "dinit"
                              "--log-file"
                              "/var/log/dinit.log"
                              "--quiet"
                              "proxy"
                            ];
                            imagePullPolicy = "Always";

                            volumeMounts = lib.mkNamedList {
                              nix-store = {
                                mountPath = "/nix";
                                subPath = "nix";
                              };

                              ssh-config.mountPath = "/etc/ssh";
                              ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                              ssh-key.mountPath = "/etc/ssh-key";
                            };
                          };
                        };
                        volumes = lib.mkNamedList {
                          nix-store.csi = {
                            driver = "nixkube";
                            readOnly = true;
                            volumeAttributes = {
                              # Only render storePaths here, building is done with a ConfigMap (config.nix) only if cfg.push is set
                              # this is so users don't have to build locally to deploy.
                              x86_64-linux = x86Pkgs.nixkube-proxy-env;
                              aarch64-linux = armPkgs.nixkube-proxy-env;
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
                  };
                }
              );
            };

          DaemonSet = lib.mapAttrs (
            n: v:
            let
              v2 = lib.recursiveUpdate v {
                labels = labels // {
                  "kubernetes.io/arch" = v.arch;
                  "nix.csi/daemonset" = n;
                };
              };
            in
            {
              metadata.labels = v2.labels;
              spec = {
                selector.matchLabels = cfg.matchLabels // {
                  "app.kubernetes.io/component" = "builder";
                  "kubernetes.io/arch" = v.arch;
                  "nix.csi/daemonset" = n;
                };
                template = lib.recursiveUpdate (podTemplate v2) {
                  spec.affinity.nodeAffinity.requiredDuringSchedulingIgnoredDuringExecution.nodeSelectorTerms = [
                    {
                      matchExpressions = [
                        {
                          key = "node-role.kubernetes.io/control-plane";
                          operator = "DoesNotExist";
                        }
                      ];
                    }
                  ];
                };
              };
            }
          ) (lib.filterAttrs (n: v: v.enable) cfg.builders.daemonsets);

          # Headless service for DNS discovery of individual builder pods
          Service.nix-csi-builders = {
            metadata.labels = labels;
            spec = {
              clusterIP = "None";
              selector = matchLabels;
              ports = lib.mkNamedList {
                ssh.port = 22;
              };
            };
          };

          Service.nix-proxy = lib.mkIf (cfg.builders.enable && cfg.builders.loadBalancerPort != null) {
            metadata.labels = labels;
            spec = {
              selector = matchLabels // {
                "nix.csi/proxy" = "true";
              };
              ports = lib.mkNamedList {
                ssh = {
                  port = cfg.builders.loadBalancerPort;
                  targetPort = "ssh";
                };
              };
              type = "LoadBalancer";
            };
          };
        };
    };
}
