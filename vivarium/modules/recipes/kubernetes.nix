# kubeadm nodes, and the facts `vivarium_runner.cluster.bring_up` needs
# about them.
#
# Every guest imports ../k8s.nix, which does nothing until a guest sets
# `services.vivarium-k8s.enable`. Those guests are the nodes. The recipe
# reads them from the evaluated guests and writes each one's CNI, image
# source and skipped addons to `settings.kubernetes.nodes`, so the cluster
# library asks the run, not the machine spec.
{
  config,
  lib,
  systems,
  ...
}:
let
  cfg = config.vivarium.recipes.kubernetes;
  nodes = lib.filterAttrs (_: system: system.config.services.vivarium-k8s.enable) systems;
in
{
  options.vivarium.recipes.kubernetes = {
    enable = lib.mkEnableOption "kubeadm nodes: every guest with `services.vivarium-k8s.enable`";
  };

  config = lib.mkIf cfg.enable {
    defaults.imports = [ ../k8s.nix ];

    settings.kubernetes.nodes = lib.mapAttrs (
      _: system:
      let
        node = system.config.services.vivarium-k8s;
      in
      {
        inherit (node) cni images skipAddons;
      }
    ) nodes;
  };
}
