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
  env = name: default: if builtins.getEnv name == "" then default else builtins.getEnv name;
  # LAB_ROUTED=vsock: `routed` is its own router, with no router guest.
  routed = env "LAB_ROUTED" "router";
  node = proxy: {
    imports = [ ../../modules/iperf3.nix ];
    services.iperf3-server.enable = true;
    vivarium.uplink.proxy = proxy;
    vivarium.inbound = [ { guest = 5201; } ];
  };
in
vivarium.lib.mkTest {
  name = "slice-iperf";
  # LAB_BACKEND=krun: both guests on krun. LAB_ROUTER=krun: the router too.
  backend = env "LAB_BACKEND" "uml";
  router.enable = routed == "router";
  router.backend = env "LAB_ROUTER" "uml";
  settings.iperf3 = pkgs.lib.getExe pkgs.iperf3;
  phases.test = {
    script = ./slice_iperf.py;
    after = [ "boot" ];
  };
  nodes.passt = node "passt";
  nodes.routed = node routed;
}
