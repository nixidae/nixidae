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
  options.nixkube.node = {
    enable = (lib.mkEnableOption "node DaemonSet (CSI driver and NRI plugin)") // {
      default = true;
    };
    compat = (lib.mkEnableOption "nix.csi.store CSI driver (for backwards compatibility)") // {
      default = true;
      apply =
        value:
        if value then
          lib.warn "nix-csi.node.compat: CSI compatibility driver (nix.csi.store) is enabled. This is deprecated and will be removed in a future release. Please migrate to the nixkube driver name." value
        else
          value;
    };
    nixConfig = lib.mkOption {
      description = "nix.conf for CSI/mounter/DaemonSet pods";
      type = (import ./nixOptions.nix) curPkgs;
    };
  };
  config =
    let
      labels = cfg.labels // {
        "app.kubernetes.io/component" = "node";
      };
      matchLabels = cfg.matchLabels // {
        "app.kubernetes.io/component" = "node";
      };
    in
    lib.mkIf cfg.enable {
      kubernetes.resources.${cfg.namespace} = {
        DaemonSet.nix-node = {
          metadata.labels = labels;
          metadata.annotations."nixkube/discard" = "true";
          spec = {
            updateStrategy = {
              type = "RollingUpdate";
              rollingUpdate.maxUnavailable = 1;
            };
            selector.matchLabels = matchLabels;
            template = {
              metadata.labels = labels;
              metadata.annotations = {
                "kubectl.kubernetes.io/default-container" = "nix-node";
                configHash = lib.hashAttrs (
                  { } // nsRes.ConfigMap.nix-node or { } // nsRes.configMap.ssh-config or { }
                );
              };
              spec = {
                serviceAccountName = "nixkube";
                priorityClassName = "system-node-critical";
                subdomain = cfg.internalServiceName;
                tolerations = [
                  {
                    key = "node-role.kubernetes.io/control-plane";
                    operator = "Exists";
                    effect = "NoSchedule";
                  }
                ];
                initContainers = lib.mkNumberedList {
                  "1" = {
                    name = "initcopy";
                    image = "ghcr.io/lillecarl/nix-csi/lix:${curPkgs.lruLix.version}-${cfg.version}";
                    imagePullPolicy = "Always";
                    securityContext.privileged = true; # chroot store
                    env = lib.mkNamedList {
                      NODE_ENV.value = builtins.toJSON (lib.mapAttrs (_: sysPkgs: "${sysPkgs.nixkube-node-env}") csiPkgs);
                    };
                    volumeMounts = lib.mkNamedList {
                      nix-store.mountPath = "/nix-volume";
                      nix-config.mountPath = "/etc/nix";

                      ssh-config.mountPath = "/etc/ssh";
                      ssh-key.mountPath = "/etc/ssh-key";
                      ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                    };
                    resources = {
                      requests = {
                        memory = "128Mi";
                        cpu = "100m";
                      };
                    };
                  };
                };
                containers = lib.mkNamedList {
                  nix-node = {
                    image = "ghcr.io/lillecarl/nix-csi/scratch:1.0.1";
                    command = [
                      "tini"
                      "--"
                      "nixkube"
                    ];
                    securityContext.privileged = true;
                    env = lib.mkNamedList {
                      BUILDERS_ENABLED.value = lib.boolToString cfg.builders.enable;
                      CACHE_ENABLED.value = lib.boolToString cfg.cache.enable;
                      ENABLE_COMPAT_DRIVER.value = lib.boolToString cfg.node.compat;
                      NRI_ENABLED.value = "true";
                      HOME.value = "/nix/var/nix-csi/root";
                      HOST_MOUNT_PATH.value = cfg.hostMountPath;
                      KUBE_NAMESPACE.valueFrom.fieldRef.fieldPath = "metadata.namespace";
                      KUBE_NODE_NAME.valueFrom.fieldRef.fieldPath = "spec.nodeName";
                      KUBE_POD_IP.valueFrom.fieldRef.fieldPath = "status.podIP";
                      KUBE_POD_NAME.valueFrom.fieldRef.fieldPath = "metadata.name";
                      KUBE_POD_UID.valueFrom.fieldRef.fieldPath = "metadata.uid";
                      NIX_BUILD_TIMEOUT.value = toString cfg.nodeBuildTimeout;
                      VERIFY_STORE_PATHS.value = lib.boolToString cfg.verifyStorePaths;
                      NIXPKGS_ALLOW_UNFREE.value = "1";
                      USER.value = "root";
                    };
                    volumeMounts = lib.mkNamedList {
                      csi-socket.mountPath = "/csi";
                      nix-config.mountPath = "/etc/nix";
                      nri-socket.mountPath = "/var/run/nri";
                      registration.mountPath = "/registration";
                      host-root = {
                        mountPath = "/host";
                      };
                      kubelet = {
                        mountPath = "/var/lib/kubelet";
                        mountPropagation = "Bidirectional";
                      };
                      nix-store = {
                        mountPath = "/nix";
                        mountPropagation = "Bidirectional";
                        subPath = "nix";
                      };

                      ssh-config.mountPath = "/etc/ssh";
                      ssh-dynauth.mountPath = "/etc/ssh-dynauth";
                      ssh-key.mountPath = "/etc/ssh-key";
                      nix-key.mountPath = "/etc/nix-key";
                    };
                    resources = {
                      requests = {
                        memory = "128Mi";
                        cpu = "100m";
                      };
                    };
                  };
                  csi-node-driver-registrar-nix-csi = lib.mkIf cfg.node.compat {
                    image = "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0";
                    args = [
                      "--v=5"
                      "--csi-address=/csi/nix.csi.store/csi.sock"
                      "--kubelet-registration-path=/var/lib/kubelet/plugins/nix.csi.store/csi.sock"
                    ];
                    env = lib.mkNamedList {
                      KUBE_NODE_NAME.valueFrom.fieldRef.fieldPath = "spec.nodeName";
                    };
                    volumeMounts = lib.mkNamedList {
                      csi-socket.mountPath = "/csi";
                      kubelet.mountPath = "/var/lib/kubelet";
                      registration.mountPath = "/registration";
                    };
                    resources = {
                      requests = {
                        memory = "10Mi";
                        cpu = "10m";
                      };
                    };
                  };
                  livenessprobe-nix-csi = lib.mkIf cfg.node.compat {
                    image = "registry.k8s.io/sig-storage/livenessprobe:v2.18.0";
                    args = [
                      "--csi-address=/csi/nix.csi.store/csi.sock"
                      "--health-port=9809"
                    ];
                    volumeMounts = lib.mkNamedList {
                      csi-socket.mountPath = "/csi";
                      registration.mountPath = "/registration";
                    };
                    resources = {
                      requests = {
                        memory = "10Mi";
                        cpu = "10m";
                      };
                    };
                  };
                  csi-node-driver-registrar-nixkube = {
                    image = "registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.16.0";
                    args = [
                      "--v=5"
                      "--csi-address=/csi/nixkube/csi.sock"
                      "--kubelet-registration-path=/var/lib/kubelet/plugins/nixkube/csi.sock"
                    ];
                    env = lib.mkNamedList {
                      KUBE_NODE_NAME.valueFrom.fieldRef.fieldPath = "spec.nodeName";
                    };
                    volumeMounts = lib.mkNamedList {
                      csi-socket.mountPath = "/csi";
                      kubelet.mountPath = "/var/lib/kubelet";
                      registration.mountPath = "/registration";
                    };
                    resources = {
                      requests = {
                        memory = "10Mi";
                        cpu = "10m";
                      };
                    };
                  };
                  livenessprobe-nixkube = {
                    image = "registry.k8s.io/sig-storage/livenessprobe:v2.18.0";
                    args = [
                      "--csi-address=/csi/nixkube/csi.sock"
                      "--health-port=9808"
                    ];
                    volumeMounts = lib.mkNamedList {
                      csi-socket.mountPath = "/csi";
                      registration.mountPath = "/registration";
                    };
                    resources = {
                      requests = {
                        memory = "10Mi";
                        cpu = "10m";
                      };
                    };
                  };
                };
                volumes = lib.mkNamedList {
                  nix-config.configMap.name = "nix-node";
                  registration.hostPath.path = "/var/lib/kubelet/plugins_registry";
                  nix-store.hostPath = {
                    path = cfg.hostMountPath;
                    type = "DirectoryOrCreate";
                  };
                  csi-socket.hostPath = {
                    path = "/var/lib/kubelet/plugins/";
                    type = "DirectoryOrCreate";
                  };
                  nri-socket.hostPath = {
                    path = "/var/run/nri";
                    type = "DirectoryOrCreate";
                  };
                  host-root.hostPath = {
                    path = "/";
                    type = "Directory";
                  };
                  kubelet.hostPath = {
                    path = "/var/lib/kubelet";
                    type = "Directory";
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
                  nix-key.secret = {
                    secretName = "nix-key";
                    defaultMode = 256; # 400
                  };
                };
              };
            };
          };
        };
        Service.${cfg.internalServiceName} = {
          metadata.labels = labels;
          spec = {
            clusterIP = "None";
            selector = matchLabels;
            ports = lib.mkNamedList {
              ssh.port = 22;
            };
          };
        };
      };
    };
}
