# Slice 1's router against passt, in one run: guest `passt` has its own
# passt, guest `routed` sits behind the run's router guest. Both on UML
# at vivarium's default MTU, measured back to back.
#
#   nix run --file lab/router/slice-iperf.nix driver -- --out DIR
{
  sources ? import ../../nix/sources.nix,
  pkgs ? import sources.nixpkgs { },
}:
let
  vivarium = import ../.. { inherit sources pkgs; };
  node = proxy: {
    imports = [ ../../modules/iperf3.nix ];
    services.iperf3-server.enable = true;
    vivarium.uplink.proxy = proxy;
    vivarium.forward = [ { ports = [ 5201 ]; } ];
  };
in
vivarium.lib.mkTest {
  name = "slice-iperf";
  router.enable = true;
  settings.iperf3 = pkgs.lib.getExe pkgs.iperf3;
  phases.test = {
    script = ./slice_iperf.py;
    after = [ "boot" ];
  };
  nodes.passt = node "passt";
  nodes.routed = node "router";
}
