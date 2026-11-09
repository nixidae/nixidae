# Does a real workload come up across three nodes?  Far heavier
# than the others: three guests, a control plane and a container
# runtime, so this one wants a builder rather than a laptop.
{
  pkgs,
  mkTest,
  k8sNodes,
  k8sConfig,
  ...
}:
mkTest {
  name = "k8s";
  phases.test = {
    script = ../k8s.py;
    after = [ "boot" ];
  };
  nodes = k8sNodes;
  vivarium.recipes.kubernetes.enable = true;
  settings = {
    inherit (k8sConfig.services.vivarium-k8s) podSubnet workloadImage;
    kubernetesVersion = pkgs.kubernetes.version;
  };
}
