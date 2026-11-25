/*
  Does a cluster with several control planes survive one of them?

  Three control planes behind the run's router (nixidae/nixidae#1592).
  The router balances the API endpoint over them; one runs `kubeadm init`
  and the others join it with `--control-plane`. tests/k8s-ha.py stops one
  API server, then the nodes and the host still reach the API through
  the endpoint, and a write still lands.

  10.112: every other cluster test has its own subnet.
*/
{
  hostPkgs,
  lib,
  k8sNodes,
  mkTest,
  ...
}:
let
  controlPlane = index: {
    imports = [ k8sNodes.cp ];
    vivarium.memory = lib.mkForce "1536M";
    vivarium.lan = lib.mkForce {
      network = "k8s-ha";
      address = "10.112.0.${toString index}/24";
    };
  };
in
mkTest {
  name = "k8s-ha";
  vivarium.recipes.kubernetes.enable = true;
  router.enable = true;
  phases.test = {
    script = ../k8s-ha.py;
    after = [ "boot" ];
  };
  nodes.cp1 = controlPlane 1;
  nodes.cp2 = controlPlane 2;
  nodes.cp3 = controlPlane 3;
  settings.kubectl = lib.getExe hostPkgs.kubectl;
}
