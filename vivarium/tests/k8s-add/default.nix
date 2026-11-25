/*
  Does a worker added to a running cluster take part in it?

  One control plane, and a template that makes a worker
  (nixidae/nixidae#1592). tests/k8s-add.py adds two instances with
  `vivarium_k8s.add_node`, checks that each registers its own address,
  that pods on every node reach each other, and removes one again.

  10.113: every other cluster test has its own subnet.
*/
{
  lib,
  k8sNodes,
  k8sConfig,
  mkTest,
  ...
}:
mkTest {
  name = "k8s-add";
  vivarium.recipes.kubernetes.enable = true;
  phases.test = {
    script = ../k8s-add.py;
    after = [ "boot" ];
  };
  nodes.cp = {
    imports = [ k8sNodes.cp ];
    services.vivarium-k8s.persistentVolumes = lib.mkForce 0;
    vivarium.lan = lib.mkForce {
      network = "k8s-add";
      address = "10.113.0.1/24";
    };
  };
  settings = { inherit (k8sConfig.services.vivarium-k8s) workloadImage; };
  segments.k8s-add.instanceAddresses = [ "10.113.0.100/24" ];
  templates.worker = {
    services.vivarium-k8s = {
      enable = true;
      role = "worker";
    };
    vivarium = {
      memory = "1280M";
      diskSize = 2048;
      lan.network = "k8s-add";
    };
  };
}
