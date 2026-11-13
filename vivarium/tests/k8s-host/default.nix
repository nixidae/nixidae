/*
  Does the host reach a cluster in the guests?

  One kubeadm node, schedulable. `bring_up` writes
  `<out>/access/kubeconfig`, aimed at the control plane's forward of
  6443, and a `kubectl` on the host must list the node with it. The
  same check is a cluster to work against:

      vivarium up checks.k8s-host --out ./k8s
      kubectl --kubeconfig ./k8s/access/kubeconfig get pods -A
      vivarium down --out ./k8s

  10.107: every other cluster test has its own subnet.
*/
{
  pkgs,
  lib,
  k8sNodes,
  mkTest,
  ...
}:
mkTest {
  name = "k8s-host";
  vivarium.recipes.kubernetes.enable = true;
  phases.test = {
    script = ../k8s-host.py;
    after = [ "boot" ];
  };
  nodes.cp = {
    imports = [ k8sNodes.cp ];
    vivarium.lan = lib.mkForce {
      network = "k8s-host";
      address = "10.107.0.1/24";
    };
  };
  settings.kubectl = "${pkgs.kubectl}/bin/kubectl";
}
