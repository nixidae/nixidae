/*
  Does a worker added at run time sit behind the router with every other
  node?

  Two control planes behind the router, which balances the API endpoint
  and runs the LoadBalancer controller, and a template that makes a
  worker. tests/k8s-add-routed.py adds one, reaches its pod from the host
  through the router by the pod's address and by a LoadBalancer
  Service's, and removes it again.

  10.114: every other cluster test has its own subnet.
*/
{
  hostPkgs,
  lib,
  k8sNodes,
  k8sConfig,
  mkTest,
  ...
}:
let
  controlPlane = index: {
    imports = [ k8sNodes.cp ];
    vivarium.memory = lib.mkForce "1536M";
    services.vivarium-k8s.persistentVolumes = lib.mkForce 0;
    vivarium.lan = lib.mkForce {
      network = "k8s-add-routed";
      address = "10.114.0.${toString index}/24";
    };
  };
in
mkTest {
  name = "k8s-add-routed";
  vivarium.recipes.kubernetes = {
    enable = true;
    loadBalancer.enable = true;
  };
  router.enable = true;
  phases.test = {
    script = ../k8s-add-routed.py;
    after = [ "boot" ];
  };
  nodes.cp1 = controlPlane 1;
  nodes.cp2 = controlPlane 2;
  settings = {
    inherit (k8sConfig.services.vivarium-k8s) workloadImage;
    curl = lib.getExe hostPkgs.curl;
  };
  segments.k8s-add-routed.instanceAddresses = [ "10.114.0.100/24" ];
  templates.worker = {
    services.vivarium-k8s = {
      enable = true;
      role = "worker";
    };
    vivarium = {
      memory = "1280M";
      diskSize = 2048;
      lan.network = "k8s-add-routed";
    };
  };
}
