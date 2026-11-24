/*
  A cluster's LoadBalancer Services, reached from the host by address and
  by name.

  One kubeadm node behind the run's router (nixidae/nixidae#1584). The
  router routes the cluster's ranges, gives the Service an address from
  its pool, and serves a SOCKS5 proxy that the host reaches through a
  forward. A second guest, `client`, reaches the Service's address
  through the router too.

  10.108: every other cluster test has its own subnet.
*/
{
  pkgs,
  hostPkgs,
  lib,
  k8sNodes,
  k8sConfig,
  mkTest,
  ...
}:
mkTest {
  name = "k8s-lb";
  vivarium.recipes.kubernetes = {
    enable = true;
    loadBalancer.enable = true;
  };
  phases.test = {
    script = ../k8s-lb.py;
    after = [ "boot" ];
  };
  nodes.cp = {
    imports = [ k8sNodes.cp ];
    vivarium.lan = lib.mkForce {
      network = "k8s-lb";
      address = "10.108.0.1/24";
    };
  };
  nodes.client.environment.systemPackages = [ pkgs.curl ];
  settings = {
    inherit (k8sConfig.services.vivarium-k8s) workloadImage;
    curl = lib.getExe hostPkgs.curl;
    kubectl = lib.getExe hostPkgs.kubectl;
  };
}
