# SPDX-License-Identifier: MIT

{
  config,
  lib,
  ...
}:
let
  cfg = config.nixkube;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.none.CSIDriver = {
      "nixkube" = {
        metadata.labels = cfg.labels;
        spec = {
          attachRequired = false;
          podInfoOnMount = true;
          volumeLifecycleModes = [ "Ephemeral" ];
          fsGroupPolicy = "File";
          requiresRepublish = false;
          storageCapacity = false;
        };
      };
      "nix.csi.store" = lib.mkIf cfg.node.compat {
        metadata.labels = cfg.labels;
        spec = {
          attachRequired = false;
          podInfoOnMount = true;
          volumeLifecycleModes = [ "Ephemeral" ];
          fsGroupPolicy = "File";
          requiresRepublish = false;
          storageCapacity = false;
        };
      };
    };
  };
}
