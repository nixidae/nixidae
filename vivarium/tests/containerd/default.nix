/*
  Does a container run at all?

  One guest, and the narrow question the cluster test answers
  only after an hour: whether the kernel has what runc wants,
  whether the images imported, and whether a container of
  symlinks can reach the store they point into.
*/
{
  pkgs,
  lib,
  mkTest,
  k8sImages,
  ...
}:
mkTest {
  name = "containerd";
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
        runtimes = [
          "crun"
          "youki"
        ]
        ++ lib.optional (config.vivarium.backend != "uml") "runsc";
      };
      vivarium = {
        memory = "1024M";
        diskSize = 2048;
        lan = {
          network = "containerd";
          address = "10.101.0.1/24";
        };
      };
    };
  settings = {
    inherit (k8sImages) sandboxImage entrypoints;
    kubernetesVersion = pkgs.kubernetes.version;
  };
}
