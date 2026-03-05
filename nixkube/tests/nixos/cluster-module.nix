# SPDX-License-Identifier: MIT

# Reusable NixOS module for a single-node kubeadm cluster.
# Adapted from hetzkube/nixos/kubernetes.nix for NixOS test VMs.
#
# Provides: containerd CRI, kubelet, kubeadm, CNI bridge config, kernel tuning.
# The kubelet starts only after kubeadm init creates /var/lib/kubelet/config.yaml.
{
  config,
  pkgs,
  lib,
  ...
}:
{
  environment.systemPackages = [
    pkgs.kubernetes # kubectl, kubeadm, kubelet
    pkgs.cri-tools # crictl
    pkgs.helix # editor
  ];

  environment.variables.EDITOR = "hx";

  # -- OpenSSH for interactive debugging --
  services.openssh = {
    enable = true;
    settings.PasswordAuthentication = true;
  };

  # -- Containerd CRI --
  virtualisation.containerd = {
    enable = true;
    settings = {
      version = lib.mkForce 3;
      # Systemd cgroups — Kubernetes will use the same
      plugins."io.containerd.grpc.v1.cri".containerd.runtimes.runc.options.SystemdCgroup = true;
      # CNI binary dir (standard path)
      plugins."io.containerd.grpc.v1.cri".cni.bin_dir = lib.mkForce "/opt/cni/bin";
      # https://github.com/containerd/cgroups/issues/378
      plugins."io.containerd.grpc.v1.cri".disable_hugetlb_controller = true;
      # Writable cgroups for systemd in containers
      plugins."io.containerd.cri.v1.runtime".containerd.runtimes.runc.cgroup_writable = true;
      # NRI plugin support
      plugins."io.containerd.nri.v1.nri" = {
        disable = false;
        socket_path = "/var/run/nri/nri.sock";
      };
    };
  };

  # -- CNI bridge config --
  # Simple bridge CNI — no cluster-level setup needed, just this config file.
  environment.etc."cni/net.d/10-bridge.conflist".text = builtins.toJSON {
    cniVersion = "1.0.0";
    name = "bridge";
    plugins = [
      {
        type = "bridge";
        bridge = "cni0";
        isGateway = true;
        ipMasq = true;
        ipam = {
          type = "host-local";
          ranges = [ [ { subnet = "10.244.0.0/16"; } ] ];
          routes = [ { dst = "0.0.0.0/0"; } ];
        };
      }
      {
        type = "portmap";
        capabilities.portMappings = true;
      }
      { type = "loopback"; }
    ];
  };

  # Install CNI binaries to standard location
  system.activationScripts.cni-install.text = ''
    ${lib.getExe pkgs.rsync} --mkpath --recursive ${pkgs.cni-plugins}/bin/ /opt/cni/bin/
  '';

  # -- Kubelet systemd service --
  # Follows the kubeadm 10-kubeadm.conf pattern.
  # Starts only when kubeadm has created /var/lib/kubelet/config.yaml.
  systemd.services.kubelet = {
    description = "kubelet: The Kubernetes Node Agent";
    wantedBy = [ "multi-user.target" ];
    after = [
      "network-online.target"
      "containerd.service"
    ];
    wants = [ "network-online.target" ];

    # Don't start until kubeadm has run
    unitConfig.ConditionPathExists = "/var/lib/kubelet/config.yaml";

    # kubelet needs mount(8)
    path = [ pkgs.util-linuxMinimal ];

    serviceConfig = {
      EnvironmentFile = [
        "-/var/lib/kubelet/kubeadm-flags.env"
        "-/etc/sysconfig/kubelet"
      ];
      ExecStart = "${lib.getExe' pkgs.kubernetes "kubelet"} $KUBELET_KUBECONFIG_ARGS $KUBELET_CONFIG_ARGS $KUBELET_KUBEADM_ARGS $KUBELET_EXTRA_ARGS";
      Restart = "on-failure";
      RestartSec = 1;
      RestartMaxDelaySec = 60;
      RestartSteps = 10;
    };

    environment = {
      KUBELET_KUBECONFIG_ARGS = "--bootstrap-kubeconfig=/etc/kubernetes/bootstrap-kubelet.conf --kubeconfig=/etc/kubernetes/kubelet.conf";
      KUBELET_CONFIG_ARGS = "--config=/var/lib/kubelet/config.yaml";
    };
  };

  # -- Kernel modules for Kubernetes networking --
  boot.kernelModules = [
    "overlay"
    "br_netfilter"
    "nf_conntrack"
  ];

  boot.kernel.sysctl = {
    # CNI bridge networking
    "net.bridge.bridge-nf-call-iptables" = 1;
    "net.bridge.bridge-nf-call-ip6tables" = 1;
    "net.ipv4.ip_forward" = 1;
    "net.ipv6.conf.all.forwarding" = 1;
    # Kubelet requirements
    "vm.overcommit_memory" = 1;
    "kernel.panic" = 10;
    "kernel.panic_on_oops" = 1;
  };

  # -- VM sizing for kubeadm cluster --
  virtualisation = {
    memorySize = 4096;
    diskSize = 10240;
    cores = 4;
  };

  # -- Interactive debug user --
  users.users.nixkube = {
    isNormalUser = true;
    initialPassword = "nixkube";
    extraGroups = [ "wheel" ];
  };

  # Allow nixkube to use sudo without password
  security.sudo.wheelNeedsPassword = false;

  # Disable COW on etcd data dir (etcd doesn't tolerate COW filesystems)
  system.activationScripts.noCOWs.text = ''
    ${lib.getExe' pkgs.coreutils "mkdir"} --parents /var/lib/etcd
    ${lib.getExe' pkgs.e2fsprogs "chattr"} -R +C /var/lib/etcd 2>/dev/null || true
  '';

  # NRI socket directory
  systemd.tmpfiles.rules = [
    "d /var/run/nri 0755 root root -"
  ];

  # Enable external networking for image pulls and nix binary cache access
  networking = {
    useDHCP = true;
    firewall.enable = false;
    nameservers = [ "9.9.9.9" ];
  };
}
