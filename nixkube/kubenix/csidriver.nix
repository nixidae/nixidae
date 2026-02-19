# SPDX-License-Identifier: MIT

{
  config,
  lib,
  ...
}:
let
  cfg = config.nix-csi;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.none.CSIDriver."nix.csi.store" = {
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
}
