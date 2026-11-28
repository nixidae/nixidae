/*
  A Kubernetes developer environment: one kubeadm node, your own app on
  it, and that app reached by its Service name over HTTPS from your
  shell and browser. See ./README.md.
*/
{
  mkTest,
  pkgs,
  lib,
  hostPkgs ? pkgs,
}:
let
  # The app's image, built by Nix. The node imports it at boot: there is
  # no registry, so the Deployment says `imagePullPolicy: Never`.
  image = pkgs.dockerTools.buildLayeredImage {
    name = "hello";
    tag = "dev";
    # Not python3Minimal: it has no `ssl` module.
    contents = [ pkgs.python3 ];
    config.Cmd = [
      "python3"
      "${./app/server.py}"
    ];
  };
in
# `image` beside the test, for `vivarium load` after an edit.
(mkTest {
  name = "k8s-devenv";

  vivarium.recipes.kubernetes = {
    enable = true;
    # LoadBalancer Services, and Service names through the router's proxy.
    loadBalancer.enable = true;
    # cert-manager with a ClusterIssuer `vivarium` on the run's lab CA.
    certManager.enable = true;
  };

  nodes.cp = {
    services.vivarium-k8s = {
      enable = true;
      role = "control-plane";
      extraImages = [ image ];
      # Keep the cluster across `vivarium down` and `vivarium up`.
      persist = true;
    };
    vivarium = {
      memory = "3072M";
      diskSize = 4096;
      # The address the node registers with: a segment of its own.
      lan = {
        network = "k8s";
        address = "10.115.0.1/24";
      };
    };
  };

  phases = {
    cluster = {
      script = ./phases/cluster.py;
      after = [ "boot" ];
    };
    deploy = {
      script = ./phases/deploy.py;
      after = [ "cluster" ];
    };
    check = {
      script = ./phases/check.py;
      after = [ "deploy" ];
    };
  };

  settings = {
    manifest = "${./app/hello.yaml}";
    kubectl = lib.getExe hostPkgs.kubectl;
    curl = lib.getExe hostPkgs.curl;
    publicRoots = "${hostPkgs.cacert}/etc/ssl/certs/ca-bundle.crt";
  };
})
// {
  inherit image;
}
