/*
  What a krun guest's vsock uplink carries (nixidae/nixidae#1404). The
  host runs the iperf3 client through the guest's forward, both
  directions. `uplink-iperf-router` measures the router's.
*/
{
  hostPkgs,
  lib,
  mkTest,
  ...
}:
mkTest {
  name = "uplink-iperf";
  backend = "krun";
  # The guest's own relay; a router guest would carry nothing.
  router.enable = false;
  settings.iperf3 = lib.getExe hostPkgs.iperf3;
  phases.test = {
    script = ../uplink-iperf.py;
    after = [ "boot" ];
  };
  nodes.vsock = {
    imports = [ ../../modules/iperf3.nix ];
    services.iperf3-server.enable = true;
    vivarium.inbound = [ { guest = 5201; } ];
  };
}
