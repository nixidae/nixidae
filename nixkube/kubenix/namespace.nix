# SPDX-License-Identifier: MIT

{
  config,
  lib,
  mkNCSI,
  ...
}:
let
  cfg = config.nix-csi;
  namespace = cfg.namespace;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.none.Namespace.${namespace} = mkNCSI { };
  };
}
