# The same questions under CRI-O. nixidae/nixidae#534.
{
  pkgs,
  mkTest,
  k8sImages,
  ...
}:
mkTest {
  name = "crio";
  vivarium.recipes.kubernetes.enable = true;
  phases.test = {
    script = ../containerd.py;
    after = [ "boot" ];
  };
  nodes.node =
    { config, ... }:
    {
      services.vivarium-k8s = {
        enable = true;
        role = "worker";
        cri = "crio";
        # Not runsc, which runs no container under CRI-O; see the
        # module's assertion.
        runtimes = [
          "crun"
          "youki"
        ];
      };
      vivarium = {
        memory = "1024M";
        diskSize = 2048;
        lan = {
          network = "crio";
          address = "10.105.0.1/24";
        };
      };
    };
  settings = {
    inherit (k8sImages) sandboxImage entrypoints;
    kubernetesVersion = pkgs.kubernetes.version;
  };
}
