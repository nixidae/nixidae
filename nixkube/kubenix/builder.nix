{
  config,
  lib,
  maybePush,
  pkgs,
  x86Pkgs,
  armPkgs,
  mkNCSI,
  ...
}:
let
  cfg = config.nix-csi;
  nsRes = config.kubernetes.resources.${cfg.namespace};
in
{
  options.nix-csi.builders = {
    enable = lib.mkEnableOption "builder pods";
    privilegedSandboxedBuilds = lib.mkOption {
      description = "To set up the sanbox Nix must run with privileges, without the sandbox Nix builds can run unprivileged";
      type = lib.types.bool;
      default = true;
    };
    nixConfig = lib.mkOption {
      description = "nix.conf for builder pods";
      type = (import ./nixOptions.nix) pkgs;
    };
    replicas = lib.mkOption {
      description = "Number of builder pod replicas";
      type = lib.types.ints.positive;
      default = 1;
    };
    resources = lib.mkOption {
      description = "Resource requests/limits for builder pods";
      type = lib.types.attrs;
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
  config =
    let
      labels = {
        "app.kubernetes.io/name" = "builder";
        "app.kubernetes.io/part-of" = "nix-csi";
      };
    in
    lib.mkIf (cfg.enable && cfg.builders.enable) {
      nix-csi.builders.nixConfig.settings.sandbox = cfg.builders.privilegedSandboxedBuilds;
      kubernetes.resources.${cfg.namespace} = {
        Deployment.nix-builder = mkNCSI {
          spec = {
            replicas = cfg.builders.replicas;
            selector.matchLabels = labels;
            template = {
              metadata.labels = labels;
              metadata.annotations = {
                "kubectl.kubernetes.io/default-container" = "nix-builder";
                configHash = lib.hashAttrs (
                  { } // nsRes.ConfigMap.nix-builder or { } // nsRes.ConfigMap.ssh-config or { }
                );
              };
              spec = {
                serviceAccountName = "nix-csi";
                initContainers = lib.mkNumberedList {
                  "1" = {
                    name = "initcopy";
                    image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
                    command = [ "initCopy" ];
                    imagePullPolicy = "Always";
                    securityContext.capabilities.add = [ "SYS_CHROOT" ]; # chroot store
                    volumeMounts = lib.mkNamedList {
                      init-store.mountPath = "/nix";
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
                      HOME.value = "/nix/var/nix-csi/root";
                      KUBE_NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                    };
                    resources = cfg.builders.resources;
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
                    driver = "nix.csi.store";
                    volumeAttributes = {
                      x86_64-linux = maybePush x86Pkgs.nix-csi-builder-env;
                      aarch64-linux = maybePush armPkgs.nix-csi-builder-env;
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
          };
        };

        # Headless service for DNS discovery of individual builder pods
        Service.nix-csi-builders = mkNCSI {
          spec = {
            clusterIP = "None";
            selector = labels;
            ports = lib.mkNamedList {
              ssh.port = 22;
            };
          };
        };
      };
    };
}
