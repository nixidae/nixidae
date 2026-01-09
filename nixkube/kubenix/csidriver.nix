{
  config,
  lib,
  mkNCSI,
  ...
}:
let
  cfg = config.nix-csi;
in
{
  config = lib.mkIf cfg.enable {
    kubernetes.resources.none.CSIDriver."nix.csi.store" = mkNCSI {
      spec = {
        attachRequired = false;
        podInfoOnMount = false;
        volumeLifecycleModes = [ "Ephemeral" ];
        fsGroupPolicy = "File";
        requiresRepublish = false;
        storageCapacity = false;
      };
    };
  };
}
